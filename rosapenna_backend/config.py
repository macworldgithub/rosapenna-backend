"""Application settings and startup security checks."""

import ipaddress
from typing import Optional
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BOOKING_", env_file=".env", extra="ignore")

    api_key: Optional[str] = None
    staff_api_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("STAFF_API_KEY", "BOOKING_STAFF_API_KEY"),
    )
    security_bypass_in_dev: bool = Field(
        default=True,
        validation_alias=AliasChoices("SECURITY_BYPASS_IN_DEV", "BOOKING_SECURITY_BYPASS_IN_DEV"),
    )
    max_request_body_bytes: int = Field(
        default=1_048_576,
        validation_alias=AliasChoices("BOOKING_MAX_REQUEST_BODY_BYTES", "MAX_REQUEST_BODY_BYTES"),
    )
    min_key_length: int = Field(
        default=12,
        validation_alias=AliasChoices("BOOKING_MIN_KEY_LENGTH", "MIN_KEY_LENGTH"),
    )
    environment: str = "development"
    host: str = "0.0.0.0"
    port: int = 4040

    default_url: str = "https://visitors.brsgolf.com/rosapenna#/course/1"

    max_concurrent_sessions: int = 3
    session_ttl_seconds: int = 30 * 60
    session_reap_interval_seconds: int = 60
    booking_fill_timeout_seconds: int = 90

    log_level: str = "INFO"
    log_file: str = "booking_api.log"

    cors_origins: str = ""
    rate_limit_per_minute: int = 20
    trusted_proxy_cidrs: str = ""
    docs_enabled: bool = True

    use_virtual_display: bool = True
    virtual_display_size: str = "1920x1080"

    enable_vnc: bool = False
    vnc_port: int = 5900
    vnc_password: Optional[str] = None

    debug_screenshots: bool = False

    teesheet_days: int = Field(365, ge=1, le=500)
    teesheet_refresh_hour: int = Field(5, ge=0, le=23)
    teesheet_refresh_enabled: bool = True
    intents_collection: str = Field("booking_intents", pattern=r"^[a-z][a-z0-9_]{0,63}$")

def validate_security_settings():
    """Reject unsafe production defaults before serving requests."""
    environment = settings.environment.strip().lower()
    if environment not in {"development", "test", "staging", "production"}:
        raise RuntimeError("BOOKING_ENVIRONMENT must be development, test, staging, or production")
    origins = [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]
    if environment == "production" and any(origin == "*" for origin in origins):
        raise RuntimeError("BOOKING_CORS_ORIGINS must list explicit origins; wildcard CORS is not supported")
    for origin in origins:
        if origin == "*":
            continue
        parsed_origin = urlsplit(origin)
        if parsed_origin.scheme not in {"http", "https"} or not parsed_origin.netloc or parsed_origin.path or parsed_origin.query or parsed_origin.fragment:
            raise RuntimeError("BOOKING_CORS_ORIGINS must contain exact origins without paths")
    if environment == "production":
        if settings.security_bypass_in_dev:
            raise RuntimeError("Production forbids SECURITY_BYPASS_IN_DEV=true; security bypass cannot be enabled in production")
        if not settings.api_key or len(settings.api_key) < settings.min_key_length:
            raise RuntimeError(f"Production requires BOOKING_API_KEY with at least {settings.min_key_length} characters")
        if not settings.staff_api_key or len(settings.staff_api_key) < settings.min_key_length:
            raise RuntimeError(f"Production requires STAFF_API_KEY with at least {settings.min_key_length} characters")
        if not origins:
            raise RuntimeError("Production requires explicit BOOKING_CORS_ORIGINS")
        if settings.docs_enabled:
            raise RuntimeError("Production requires BOOKING_DOCS_ENABLED=false")
        if settings.enable_vnc:
            raise RuntimeError("BOOKING_ENABLE_VNC must remain false in production")
        if any(urlsplit(origin).scheme != "https" for origin in origins):
            raise RuntimeError("Production CORS origins must use HTTPS")
    if settings.rate_limit_per_minute <= 0:
        raise RuntimeError("BOOKING_RATE_LIMIT_PER_MINUTE must be greater than zero")
    for cidr in filter(None, (value.strip() for value in settings.trusted_proxy_cidrs.split(","))):
        try:
            ipaddress.ip_network(cidr, strict=False)
        except ValueError as exc:
            raise RuntimeError("BOOKING_TRUSTED_PROXY_CIDRS contains an invalid network") from exc

settings = Settings()
