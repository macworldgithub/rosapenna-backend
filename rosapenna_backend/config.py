"""Application settings and startup security checks."""

import ipaddress
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from rosapenna_backend.core.settings_env import ENV_FILE

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BOOKING_", env_file=ENV_FILE, extra="ignore")

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
    staff_brs_preview_storage_state: Optional[Path] = None

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

    teesheet_days: int = Field(365, ge=1, le=500)
    teesheet_refresh_hour: int = Field(5, ge=0, le=23)
    teesheet_refresh_enabled: bool = True
    intents_collection: str = Field("booking_intents", pattern=r"^[a-z][a-z0-9_]{0,63}$")

    payment_mode: str = Field(
        default="disabled",
        validation_alias=AliasChoices("BOOKING_PAYMENT_MODE", "PAYMENT_MODE"),
    )
    simulation_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("BOOKING_SIMULATION_ENABLED", "SIMULATION_ENABLED"),
    )
    simulation_signing_secret: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("BOOKING_SIMULATION_SIGNING_SECRET", "SIMULATION_SIGNING_SECRET"),
    )
    simulation_run_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("BOOKING_SIMULATION_RUN_ID", "SIMULATION_RUN_ID"),
    )

    confirmations_mode: str = Field(
        default="disabled",
        validation_alias=AliasChoices("BOOKING_CONFIRMATIONS_MODE", "CONFIRMATIONS_MODE"),
    )
    confirmations_test_run_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("BOOKING_CONFIRMATIONS_TEST_RUN_ID", "CONFIRMATIONS_TEST_RUN_ID"),
    )
    confirmations_recording_dir: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("BOOKING_CONFIRMATIONS_RECORDING_DIR", "CONFIRMATIONS_RECORDING_DIR"),
    )
    confirmations_staff_api_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("BOOKING_CONFIRMATIONS_STAFF_API_ENABLED", "CONFIRMATIONS_STAFF_API_ENABLED"),
    )

    pre_arrival_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("BOOKING_PRE_ARRIVAL_ENABLED", "PRE_ARRIVAL_ENABLED"),
    )
    pre_arrival_mode: str = Field(
        default="disabled",
        validation_alias=AliasChoices("BOOKING_PRE_ARRIVAL_MODE", "PRE_ARRIVAL_MODE"),
    )
    pre_arrival_test_run_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("BOOKING_PRE_ARRIVAL_TEST_RUN_ID", "PRE_ARRIVAL_TEST_RUN_ID"),
    )
    pre_arrival_recording_dir: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("BOOKING_PRE_ARRIVAL_RECORDING_DIR", "PRE_ARRIVAL_RECORDING_DIR"),
    )
    pre_arrival_policy_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("BOOKING_PRE_ARRIVAL_POLICY_ID", "PRE_ARRIVAL_POLICY_ID"),
    )
    pre_arrival_policy_version: Optional[int] = Field(
        default=None,
        validation_alias=AliasChoices("BOOKING_PRE_ARRIVAL_POLICY_VERSION", "PRE_ARRIVAL_POLICY_VERSION"),
    )
    pre_arrival_staff_api_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("BOOKING_PRE_ARRIVAL_STAFF_API_ENABLED", "PRE_ARRIVAL_STAFF_API_ENABLED"),
    )

    balance_payments_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("BOOKING_BALANCE_PAYMENTS_ENABLED", "BALANCE_PAYMENTS_ENABLED"),
    )
    balance_payment_mode: str = Field(
        default="disabled",
        validation_alias=AliasChoices("BOOKING_BALANCE_PAYMENT_MODE", "BALANCE_PAYMENT_MODE"),
    )
    balance_staff_api_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("BOOKING_BALANCE_STAFF_API_ENABLED", "BALANCE_STAFF_API_ENABLED"),
    )
    balance_provider_ingress_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("BOOKING_BALANCE_PROVIDER_INGRESS_ENABLED", "BALANCE_PROVIDER_INGRESS_ENABLED"),
    )
    balance_payment_policy_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("BOOKING_BALANCE_PAYMENT_POLICY_ID", "BALANCE_PAYMENT_POLICY_ID"),
    )
    balance_payment_policy_version: Optional[int] = Field(
        default=None,
        validation_alias=AliasChoices("BOOKING_BALANCE_PAYMENT_POLICY_VERSION", "BALANCE_PAYMENT_POLICY_VERSION"),
    )
    balance_payment_test_run_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("BOOKING_BALANCE_PAYMENT_TEST_RUN_ID", "BALANCE_PAYMENT_TEST_RUN_ID"),
    )
    balance_payment_recording_dir: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("BOOKING_BALANCE_PAYMENT_RECORDING_DIR", "BALANCE_PAYMENT_RECORDING_DIR"),
    )
    balance_payment_signing_secret: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("BOOKING_BALANCE_PAYMENT_SIGNING_SECRET", "BALANCE_PAYMENT_SIGNING_SECRET"),
    )
    balance_payment_property_id: str = Field(
        default="ROSA",
        validation_alias=AliasChoices("BOOKING_BALANCE_PAYMENT_PROPERTY_ID", "BALANCE_PAYMENT_PROPERTY_ID"),
    )

def validate_security_settings():
    """Reject unsafe production defaults before serving requests."""
    import os

    environment = settings.environment.strip().lower()
    if environment not in {"development", "test", "staging", "production"}:
        raise RuntimeError("BOOKING_ENVIRONMENT must be development, test, staging, or production")

    payment_mode = settings.payment_mode.strip().lower()
    if payment_mode not in {"disabled", "simulation", "prommt_test"}:
        raise RuntimeError("BOOKING_PAYMENT_MODE must be 'disabled', 'simulation', or 'prommt_test'")
    if payment_mode == "prommt_test":
        from rosapenna_backend.database import MongoSettings

        mongo = MongoSettings()
        if (
            environment != "test"
            or settings.host != "127.0.0.1"
            or mongo.uri.get_secret_value() != "mongodb://127.0.0.1:27017"
            or not (mongo.database or "").startswith("rosapenna_prommt_test_")
        ):
            raise RuntimeError("Prommt test checkout requires a loopback test service and a dedicated local test database")

    if settings.simulation_enabled != (payment_mode == "simulation"):
        raise RuntimeError("Simulation requires both payment mode and simulation enablement to agree")

    if settings.simulation_enabled and environment in {"staging", "production"}:
        raise RuntimeError("BOOKING_SIMULATION_ENABLED cannot be true in staging or production")

    if payment_mode == "simulation" and environment in {"staging", "production"}:
        raise RuntimeError("Simulation payment mode is forbidden in staging or production")

    confirmations_mode = settings.confirmations_mode.strip().lower()
    if confirmations_mode not in {"disabled", "recording"}:
        raise RuntimeError("BOOKING_CONFIRMATIONS_MODE must be 'disabled' or 'recording'")

    if confirmations_mode == "recording" and environment in {"staging", "production"}:
        raise RuntimeError("Recording confirmations mode is forbidden in staging or production")

    if confirmations_mode == "recording":
        if environment != "test":
            raise RuntimeError("Recording confirmations mode is allowed only in test")
        if not settings.confirmations_test_run_id or len(settings.confirmations_test_run_id) > 100:
            raise RuntimeError("BOOKING_CONFIRMATIONS_TEST_RUN_ID is required for recording mode")
        if not settings.confirmations_recording_dir:
            raise RuntimeError("BOOKING_CONFIRMATIONS_RECORDING_DIR is required for recording mode")
        recording_path = Path(settings.confirmations_recording_dir).expanduser()
        if not recording_path.is_absolute() or recording_path.is_symlink():
            raise RuntimeError("BOOKING_CONFIRMATIONS_RECORDING_DIR must be an absolute non-symlink path")
        if payment_mode == "simulation" and settings.confirmations_test_run_id != settings.simulation_run_id:
            raise RuntimeError("N07 and simulated payments must use the same test run ID")

    if settings.confirmations_staff_api_enabled and confirmations_mode != "recording":
        raise RuntimeError("BOOKING_CONFIRMATIONS_STAFF_API_ENABLED requires confirmations_mode to be 'recording'")
    if settings.confirmations_staff_api_enabled and environment != "test":
        raise RuntimeError("N07 staff confirmation routes are allowed only in test")

    pre_arrival_mode = settings.pre_arrival_mode.strip().lower()
    if pre_arrival_mode not in {"disabled", "recording", "live"}:
        raise RuntimeError("BOOKING_PRE_ARRIVAL_MODE must be 'disabled', 'recording', or 'live'")

    if pre_arrival_mode == "recording" and environment in {"staging", "production"}:
        raise RuntimeError("Recording pre-arrival mode is forbidden in staging or production")

    if pre_arrival_mode == "recording":
        if environment != "test":
            raise RuntimeError("Recording pre-arrival mode is allowed only in test")
        if not settings.pre_arrival_test_run_id or len(settings.pre_arrival_test_run_id) > 100:
            raise RuntimeError("BOOKING_PRE_ARRIVAL_TEST_RUN_ID is required for recording mode")
        if not settings.pre_arrival_recording_dir:
            raise RuntimeError("BOOKING_PRE_ARRIVAL_RECORDING_DIR is required for recording mode")
        pre_rec_path = Path(settings.pre_arrival_recording_dir).expanduser()
        if not pre_rec_path.is_absolute() or pre_rec_path.is_symlink():
            raise RuntimeError("BOOKING_PRE_ARRIVAL_RECORDING_DIR must be an absolute non-symlink path")

    if settings.pre_arrival_staff_api_enabled and pre_arrival_mode != "recording":
        raise RuntimeError("BOOKING_PRE_ARRIVAL_STAFF_API_ENABLED requires pre_arrival_mode to be 'recording'")
    if settings.pre_arrival_staff_api_enabled and environment != "test":
        raise RuntimeError("N08 staff pre-arrival routes are allowed only in test")

    balance_payment_mode = settings.balance_payment_mode.strip().lower()
    if balance_payment_mode not in {"disabled", "simulation"}:
        raise RuntimeError("BOOKING_BALANCE_PAYMENT_MODE must be 'disabled' or 'simulation'")

    if balance_payment_mode == "simulation":
        if environment in {"staging", "production"}:
            raise RuntimeError("Simulation balance payment mode is forbidden in staging or production")
        if not settings.balance_payments_enabled:
            raise RuntimeError("Simulation balance mode requires BOOKING_BALANCE_PAYMENTS_ENABLED=true")

    if settings.balance_staff_api_enabled and environment in {"staging", "production"}:
        raise RuntimeError("N09 staff balance payment routes are forbidden in staging or production")

    if settings.balance_provider_ingress_enabled:
        raise RuntimeError("BOOKING_BALANCE_PROVIDER_INGRESS_ENABLED must remain false until verified contract is authorized")

    for live_flag in ("PROMMT_ALLOW_LIVE_CALLS", "OPERA_ALLOW_LIVE_WRITES", "BRS_ALLOW_LIVE_WRITES", "EMAIL_ALLOW_LIVE_SEND"):
        if os.getenv(live_flag, "").strip().lower() in {"true", "1", "yes"} and environment in {"development", "test"}:
            raise RuntimeError(f"{live_flag}=true is strictly forbidden in development or test environments")

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
