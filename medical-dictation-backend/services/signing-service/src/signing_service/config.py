"""signing-service configuration."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env.local",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    service_name: str = "signing-service"
    environment: str = Field(default="development", alias="ENVIRONMENT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    testing: bool = Field(default=False, alias="TESTING")

    otel_exporter_otlp_endpoint: str = Field(
        default="http://localhost:4317", alias="OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    otel_sdk_disabled: bool = Field(default=False, alias="OTEL_SDK_DISABLED")

    # Internal-API auth (service-to-service JWTs).
    auth_issuer: str = Field(
        default="http://localhost:8088/realms/medical-dictation",
        alias="AUTH_ISSUER",
    )
    auth_jwks_url: str = Field(
        default="http://localhost:8088/realms/medical-dictation/protocol/openid-connect/certs",
        alias="AUTH_JWKS_URL",
    )
    auth_audience: str = Field(default="mdx-api", alias="AUTH_AUDIENCE")
    auth_clock_skew_seconds: int = Field(default=30, alias="AUTH_CLOCK_SKEW_SECONDS")

    # ── CORS (SPA integration) ──────────────────────────────────────────
    # Comma-separated browser origins allowed to call this service WITH
    # credentials (the HttpOnly refresh cookie). Must be explicit origins —
    # never "*" — because allow_credentials=True forbids the wildcard. Mirror
    # of the auth-service allow-list (sprint A3).
    cors_allowed_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173,http://127.0.0.1:4173",
        alias="CORS_ALLOWED_ORIGINS",
    )

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    # DB pools.
    db_app_role_dsn: str = Field(
        default="postgresql://app_role:app_role@localhost:5432/medical_dictation",
        alias="DB_APP_ROLE_DSN",
    )
    db_audit_writer_dsn: str = Field(
        default="postgresql://audit_writer:audit_writer@localhost:5432/medical_dictation",
        alias="DB_AUDIT_WRITER_DSN",
    )
    db_public_verify_dsn: str = Field(
        default="postgresql://app_public_verify:app_public_verify@localhost:5432/medical_dictation",
        alias="DB_PUBLIC_VERIFY_DSN",
    )
    db_callback_writer_dsn: str = Field(
        default="postgresql://app_callback_writer:app_callback_writer@localhost:5432/medical_dictation",
        alias="DB_CALLBACK_WRITER_DSN",
    )
    db_pool_min_size: int = Field(default=1, alias="DB_POOL_MIN_SIZE")
    db_pool_max_size: int = Field(default=8, alias="DB_POOL_MAX_SIZE")

    # Redis (rate limiter for /verify).
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")

    # System HMAC keys (rotated yearly; sprint-17 will rotate via KMS).
    signer_ipn_hmac_key_hex: str = Field(default="00" * 32, alias="SIGNER_IPN_HMAC_KEY")
    public_verify_ip_hmac_key_hex: str = Field(default="11" * 32, alias="PUBLIC_VERIFY_IP_HMAC_KEY")

    # Trust store directory (PEM bundles).
    trust_store_dir: Path = Field(default=Path("infra/trust-store"), alias="TRUST_STORE_DIR")
    trust_store_include_test_ca: bool = Field(default=False, alias="TRUST_STORE_INCLUDE_TEST_CA")

    # Provider config.
    diia_base_url: str = Field(default="", alias="DIIA_BASE_URL")
    diia_api_token: str = Field(default="", alias="DIIA_API_TOKEN")
    iit_helper_health_url: str = Field(default="", alias="IIT_HELPER_HEALTH_URL")
    iit_callback_hmac_key_hex: str = Field(default="22" * 32, alias="IIT_CALLBACK_HMAC_KEY")

    # Public verify rate limit.
    public_verify_rate_per_minute: int = Field(default=60, alias="PUBLIC_VERIFY_RATE_PER_MINUTE")

    # Allow mock provider — refused in production by libs/kep, but
    # this flag controls whether we even wire it.
    enable_mock_provider: bool = Field(default=True, alias="ENABLE_MOCK_PROVIDER")

    # Max size of a locally-signed PDF upload (M1·B4).
    max_upload_mb: int = Field(default=25, alias="SIGNING_MAX_UPLOAD_MB")

    # ── file_key provider (UAPKI backend, ADR-0026) ─────────────────────
    # Wired only when the UAPKI shared objects are present at uapki_lib_dir.
    uapki_lib_dir: Path = Field(default=Path("/opt/uapki"), alias="UAPKI_LIB_DIR")
    uapki_cert_cache_dir: Path = Field(
        default=Path("/tmp/uapki-cert-cache"), alias="UAPKI_CERT_CACHE_DIR"
    )
    uapki_crl_cache_dir: Path = Field(
        default=Path("/tmp/uapki-crl-cache"), alias="UAPKI_CRL_CACHE_DIR"
    )
    uapki_tsp_url: str = Field(default="", alias="UAPKI_TSP_URL")
    uapki_offline: bool = Field(default=True, alias="UAPKI_OFFLINE")
    # Max accepted key-container upload (containers are ~2-8 KB).
    max_key_container_kb: int = Field(default=64, alias="SIGNING_MAX_KEY_CONTAINER_KB")

    # ── dev_password provider — DEVELOPMENT ONLY ─────────────────────────
    # Guard 2 of 3: this flag is REJECTED outright in production (below);
    # guard 1 is DevPasswordProvider's own constructor, guard 3 is the CI
    # gate `check-no-dev-signing-in-prod-config`.
    enable_dev_password_provider: bool = Field(
        default=False, alias="SIGNING_DEV_PASSWORD_ENABLED"
    )
    # Keycloak confidential client for the password re-auth grant — the
    # SAME client login uses (env names + defaults mirror auth-service).
    keycloak_client_id: str = Field(default="mdx-backend", alias="KEYCLOAK_LOGIN_CLIENT_ID")
    keycloak_client_secret: str = Field(
        default="dev-secret-change-in-prod-mdx-backend",
        alias="KEYCLOAK_LOGIN_CLIENT_SECRET",
    )

    @model_validator(mode="after")
    def _reject_dev_password_in_production(self) -> Settings:
        if self.enable_dev_password_provider and self.environment.lower() in (
            "production",
            "prod",
        ):
            raise ValueError(
                "SIGNING_DEV_PASSWORD_ENABLED must never be set in production — "
                "the dev_password provider is a development-only scaffold "
                "(see docs/adr/0026 and the sprint-09 revision spec)."
            )
        return self


settings = Settings()
