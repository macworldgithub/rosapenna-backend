"""Prommt payment gateway client (Ticket F11).

Honors zero live calls by default (allow_live_calls=False) to safeguard production credentials.
Provides verified sandbox mock responses for testing and development.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional, Union

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from rosapenna_backend.core.settings_env import ENV_FILE

from rosapenna_backend.core.logging import logger
from rosapenna_backend.integrations.prommt.schemas import (
    PrommtCreatePaymentRequest,
    PrommtPaymentDetailsResponse,
    PrommtPaymentLinkResponse,
    PrommtPaymentRequestResponse,
    PrommtWebhookPayload,
)


class PrommtSettings(BaseSettings):
    """Prommt payment gateway configuration."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE, extra="ignore", hide_input_in_errors=True
    )

    api_key: SecretStr = Field(SecretStr(""), validation_alias="PROMMT_API_KEY")
    webhook_secret: SecretStr = Field(SecretStr(""), validation_alias="PROMMT_WEBHOOK_SECRET")
    base_url: str = Field("https://gw.prommt.com/api/v1", validation_alias="PROMMT_BASE_URL")
    mode: str = Field("TEST", validation_alias="PROMMT_MODE")
    location_id: Optional[int] = Field(None, validation_alias="PROMMT_LOCATION_ID")
    timeout_seconds: int = Field(15, ge=2, le=60)
    allow_live_calls: bool = Field(False, validation_alias="PROMMT_ALLOW_LIVE_CALLS")


class PrommtError(Exception):
    """Base exception for Prommt integration errors."""

    def __init__(self, message: str, status_code: int = 502, detail_code: str = "prommt_error"):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.detail_code = detail_code


class PrommtAuthError(PrommtError):
    def __init__(self, message: str = "Prommt API authentication failed"):
        super().__init__(message, status_code=401, detail_code="prommt_auth_error")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PrommtClient:
    """Client for interacting with the Prommt API gateway."""

    def __init__(self, settings: Optional[PrommtSettings] = None):
        self.settings = settings or PrommtSettings()

    def create_test_checkout_link(self, request: PrommtCreatePaymentRequest) -> PrommtPaymentRequestResponse:
        """Create a link-only request and verify the exact request appears in Prommt TEST."""
        from rosapenna_backend.config import settings as app_settings

        origin = "https://gw.prommt.com/api/v1"
        key = self.settings.api_key.get_secret_value().strip()
        if (
            app_settings.environment != "test"
            or app_settings.payment_mode != "prommt_test"
            or self.settings.mode != "TEST"
            or self.settings.base_url.rstrip("/") != origin
            or not key
            or request.currency != "EUR"
            or not request.external_id
        ):
            raise PrommtError("Prommt test checkout is unavailable.", status_code=503)

        opener = urllib.request.build_opener(_NoRedirect())
        headers = {"Authorization": f"apikey {key}", "Accept": "application/json", "Content-Type": "application/json"}

        def call(path: str, data: Optional[bytes] = None) -> dict:
            req = urllib.request.Request(origin + path, data=data, headers=headers)
            with opener.open(req, timeout=self.settings.timeout_seconds) as response:
                return json.load(response)

        # Link-only mode does not select an email or SMS delivery channel.
        payload = {
            "amount": request.amount,
            "currency": "EUR",
            "channel": "LINK",
            "confirmation": False,
            "amountEditable": False,
            "payerFirstName": request.payer_first_name,
            "payerLastName": request.payer_last_name,
            "payerEmail": request.payer_email,
            "description": request.description,
            "externalId": request.external_id,
            "customParams": {"Confirmation Number:": request.external_id},
        }

        try:
            created = PrommtPaymentRequestResponse.model_validate(call("/paymentrequests", json.dumps(payload).encode()))
            records = call("/paymentrequests?mode=TEST").get("content", [])
            matching_records = [
                record for record in records
                if isinstance(record, dict) and record.get("id") == created.resource_id
                and record.get("externalId") == request.external_id
            ]
            if len(matching_records) != 1:
                raise PrommtError("Prommt did not verify this request as TEST. The link was withheld.")
            record = matching_records[0]
            try:
                amount_matches = Decimal(str(record.get("requestedAmount"))) == Decimal(str(request.amount))
            except InvalidOperation:
                amount_matches = False
            if record.get("currency") != request.currency or not amount_matches:
                raise PrommtError("Prommt TEST amount or currency did not match. The link was withheld.")
            resource = urllib.parse.quote(created.resource_id, safe="")
            url = created.payment_url or call(f"/paymentrequests/{resource}/link").get("url")
            parts = urllib.parse.urlsplit(url or "")
            if (
                parts.scheme != "https"
                or parts.hostname not in {"prommt.com", "pay.prommt.com"}
                or parts.port not in {None, 443}
                or parts.username is not None
                or parts.password is not None
            ):
                raise PrommtError("Prommt did not return a valid hosted payment link.")
            return created.model_copy(update={"payment_url": url})
        except PrommtError:
            raise
        except urllib.error.HTTPError as err:
            raise PrommtError(f"Prommt TEST returned HTTP {err.code}. The link was withheld.") from None
        except (ValueError, TypeError, AttributeError, urllib.error.URLError):
            raise PrommtError("Prommt could not create or verify the test payment link.") from None

    def create_payment_request(
        self,
        request: PrommtCreatePaymentRequest,
    ) -> PrommtPaymentRequestResponse:
        """Create a payment request in Prommt.

        If allow_live_calls is False, returns a deterministic sandbox mock response
        to prevent hitting the live Prommt production account.
        """
        # Strict protection: do not make real HTTP calls if live calls are not explicitly enabled
        import os
        from rosapenna_backend.config import settings as app_settings

        env_name = getattr(app_settings, "environment", "development").strip().lower()
        if self.settings.allow_live_calls:
            if env_name in {"development", "test"} or os.getenv("PROMMT_ALLOW_LIVE_CALLS", "").strip().lower() in {"false", "0", "no", ""}:
                raise PrommtError("Live outbound Prommt calls are strictly forbidden in development or test environment profiles.", status_code=403)

        if not self.settings.allow_live_calls:
            resource_id = f"pmt_req_{uuid.uuid4().hex[:12]}"
            # Must point to local route, never public pay.prommt.com
            payment_url = f"/dev/payments/{resource_id}"
            logger.info(
                "Prommt live calls disabled; returning safe local sandbox payment request %s",
                resource_id,
            )
            return PrommtPaymentRequestResponse(
                resourceId=resource_id,
                status="CREATED",
                message="Sandbox payment request created",
                url=payment_url,
            )

        # Real live call path (only if user explicitly enables allow_live_calls and environment permits)
        url = f"{self.settings.base_url.rstrip('/')}/paymentrequests"
        payload_dict = request.model_dump(by_alias=True, exclude_none=True)
        if self.settings.location_id and "locationId" not in payload_dict:
            payload_dict["locationId"] = self.settings.location_id

        data = json.dumps(payload_dict).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"apikey {self.settings.api_key.get_secret_value()}",
                "User-Agent": "Rosapenna-Booking/1.0",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.settings.timeout_seconds) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return PrommtPaymentRequestResponse.model_validate(body)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise PrommtAuthError(f"Prommt authentication failed (HTTP {exc.code})") from exc
            err_body = exc.read().decode("utf-8", errors="replace")
            logger.error("Prommt API error: HTTP %d: %s", exc.code, err_body)
            raise PrommtError(f"Prommt API error (HTTP {exc.code}): {err_body}", status_code=exc.code) from exc
        except Exception as exc:
            logger.error("Prommt network error: %s", exc)
            raise PrommtError(f"Network error connecting to Prommt: {exc}") from exc

    def get_payment_link(self, payment_request_id: str) -> str:
        """Get the payment link URL for a given payment request ID."""
        if not self.settings.allow_live_calls:
            return f"/dev/payments/{payment_request_id}"

        import os
        from rosapenna_backend.config import settings as app_settings

        env_name = getattr(app_settings, "environment", "development").strip().lower()
        if env_name in {"development", "test"} or os.getenv("PROMMT_ALLOW_LIVE_CALLS", "").strip().lower() in {"false", "0", "no", ""}:
            raise PrommtError("Live outbound Prommt calls are strictly forbidden in development or test environment profiles.", status_code=403)

        url = f"{self.settings.base_url.rstrip('/')}/paymentrequests/{urllib.parse.quote(payment_request_id)}/link"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"apikey {self.settings.api_key.get_secret_value()}",
                "User-Agent": "Rosapenna-Booking/1.0",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.settings.timeout_seconds) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return body.get("url") or f"https://pay.prommt.com/link/{payment_request_id}"
        except Exception as exc:
            logger.warning("Failed to fetch Prommt link for %s: %s", payment_request_id, exc)
            return f"https://pay.prommt.com/link/{payment_request_id}"

    def get_test_payment_details(self, payment_request_id: str) -> PrommtPaymentDetailsResponse:
        """Read a request in the isolated Prommt TEST checkout profile."""
        from rosapenna_backend.config import settings as app_settings

        origin = "https://gw.prommt.com/api/v1"
        key = self.settings.api_key.get_secret_value().strip()
        if (
            app_settings.environment != "test"
            or app_settings.payment_mode != "prommt_test"
            or self.settings.mode != "TEST"
            or self.settings.allow_live_calls
            or self.settings.base_url.rstrip("/") != origin
            or not key
        ):
            raise PrommtError("Prommt TEST payment lookup is unavailable.", status_code=503)
        resource = urllib.parse.quote(payment_request_id, safe="")
        req = urllib.request.Request(
            f"{origin}/paymentrequests/{resource}",
            headers={"Authorization": f"apikey {key}", "Accept": "application/json"},
        )
        try:
            with urllib.request.build_opener(_NoRedirect()).open(req, timeout=self.settings.timeout_seconds) as response:
                return PrommtPaymentDetailsResponse.model_validate(json.load(response))
        except (ValueError, urllib.error.URLError):
            raise PrommtError("Prommt TEST payment details could not be verified.") from None

    def get_payment_details(self, payment_request_id: str) -> PrommtPaymentDetailsResponse:
        """Fetch payment request details from Prommt."""
        if not self.settings.allow_live_calls:
            return PrommtPaymentDetailsResponse(
                transactionId=payment_request_id,
                status="CREATED",
                channel="LINK",
            )

        import os
        from rosapenna_backend.config import settings as app_settings

        env_name = getattr(app_settings, "environment", "development").strip().lower()
        if env_name in {"development", "test"} or os.getenv("PROMMT_ALLOW_LIVE_CALLS", "").strip().lower() in {"false", "0", "no", ""}:
            raise PrommtError("Live outbound Prommt calls are strictly forbidden in development or test environment profiles.", status_code=403)

        url = f"{self.settings.base_url.rstrip('/')}/paymentrequests/{urllib.parse.quote(payment_request_id)}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"apikey {self.settings.api_key.get_secret_value()}",
                "User-Agent": "Rosapenna-Booking/1.0",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.settings.timeout_seconds) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return PrommtPaymentDetailsResponse.model_validate(body)
        except Exception as exc:
            raise PrommtError(f"Failed to fetch payment details: {exc}") from exc

    def verify_webhook_signature(
        self,
        raw_body: bytes,
        signature_header: str,
        secret_override: Optional[str] = None,
    ) -> bool:
        """Verify the Prommt HMAC-SHA256 signature provided in the X-Signature header."""
        secret = secret_override or self.settings.webhook_secret.get_secret_value()
        if not secret or not signature_header:
            return False

        computed = hmac.new(
            secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()

        signature = signature_header.strip().lower()
        return signature.isascii() and hmac.compare_digest(computed, signature)

    def verify_timestamp(self, timestamp: Union[int, str], max_skew_seconds: int = 300) -> bool:
        """Verify that the webhook timestamp is within the allowable skew window (default 300s)."""
        try:
            ts = int(timestamp)
        except (ValueError, TypeError):
            return False
        # The Prommt field table uses milliseconds and its sample uses seconds.
        if ts >= 10**12:
            ts /= 1000
        now_ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
        return abs(now_ts - ts) <= max_skew_seconds
