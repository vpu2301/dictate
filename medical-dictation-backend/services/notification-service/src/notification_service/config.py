"""Typed configuration. The ONLY module allowed to read the environment."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env.local",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    service_name: str = "notification-service"
    environment: str = Field(default="development", alias="ENVIRONMENT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    testing: bool = Field(default=False, alias="TESTING")

    otel_exporter_otlp_endpoint: str = Field(
        default="http://localhost:4317",
        alias="OTEL_EXPORTER_OTLP_ENDPOINT",
    )
    otel_sdk_disabled: bool = Field(default=False, alias="OTEL_SDK_DISABLED")

    auth_bypass_dev: bool = Field(default=False, alias="AUTH_BYPASS_DEV")

    # ── Auth ─────────────────────────────────────────────────────────
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

    cors_allowed_origins: str = Field(default="", alias="CORS_ALLOWED_ORIGINS")

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    # ── Database ─────────────────────────────────────────────────────
    db_app_role_dsn: str = Field(
        default="postgresql://app_role:app_role@localhost:5432/medical_dictation",
        alias="DB_APP_ROLE_DSN",
    )
    db_audit_writer_dsn: str = Field(
        default="postgresql://audit_writer:audit_writer@localhost:5432/medical_dictation",
        alias="DB_AUDIT_WRITER_DSN",
    )
    db_pool_min_size: int = Field(default=1, alias="DB_POOL_MIN_SIZE")
    db_pool_max_size: int = Field(default=10, alias="DB_POOL_MAX_SIZE")

    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")

    # ── Ingest ───────────────────────────────────────────────────────
    # Consumer identity within the group. MUST be unique per replica —
    # two replicas sharing a name share a pending-entries list, so a
    # crash on one silently hands the other's in-flight work to it.
    consumer_name: str = Field(default="notification-1", alias="MDX_NOTIFICATION_CONSUMER_NAME")
    ingest_enabled: bool = Field(default=True, alias="MDX_NOTIFICATION_INGEST")
    ingest_max_retries: int = Field(default=3, alias="MDX_NOTIFICATION_INGEST_MAX_RETRIES")
    ingest_reclaim_idle_ms: int = Field(
        default=60_000, alias="MDX_NOTIFICATION_INGEST_RECLAIM_IDLE_MS"
    )

    # ── Storm control (E1) ───────────────────────────────────────────
    # A misbehaving producer — or a reconciler flagging thousands of rows
    # at once — must not be able to bury every user's feed. Beyond the
    # cap, same-category events inside the window coalesce into one
    # "N reports finalized" row instead of N rows.
    rate_cap_per_category: int = Field(default=20, alias="MDX_NOTIFICATION_RATE_CAP")
    rate_cap_window_s: int = Field(default=300, alias="MDX_NOTIFICATION_RATE_WINDOW_S")

    # ── Background workers ───────────────────────────────────────────
    background_jobs_enabled: bool = Field(default=True, alias="MDX_BACKGROUND_JOBS")
    delivery_poll_interval_s: float = Field(
        default=2.0, alias="MDX_NOTIFICATION_DELIVERY_INTERVAL_S"
    )
    delivery_batch_size: int = Field(default=50, alias="MDX_NOTIFICATION_DELIVERY_BATCH")
    delivery_max_attempts: int = Field(default=5, alias="MDX_NOTIFICATION_DELIVERY_MAX_ATTEMPTS")
    delivery_backoff_base_s: float = Field(
        default=30.0, alias="MDX_NOTIFICATION_DELIVERY_BACKOFF_BASE_S"
    )

    # ── Email ────────────────────────────────────────────────────────
    email_provider: str = Field(default="mock", alias="MDX_EMAIL_PROVIDER")  # smtp | mock
    email_from: str = Field(default="noreply@medical-dictation.local", alias="MDX_EMAIL_FROM")
    email_from_name: str = Field(default="Medical Dictation", alias="MDX_EMAIL_FROM_NAME")
    smtp_host: str = Field(default="localhost", alias="MDX_SMTP_HOST")
    smtp_port: int = Field(default=1025, alias="MDX_SMTP_PORT")
    smtp_use_tls: bool = Field(default=False, alias="MDX_SMTP_USE_TLS")
    smtp_username: str = Field(default="", alias="MDX_SMTP_USERNAME")
    smtp_password: str = Field(default="", alias="MDX_SMTP_PASSWORD")

    # Base URL the deep links point at — the SPA origin, not this service.
    app_base_url: str = Field(default="http://localhost:5173", alias="MDX_APP_BASE_URL")

    @property
    def is_production(self) -> bool:
        return self.environment in {"production", "staging"}

    # ── Session revocation check (sprint 16) ────────────────────────────
    # When on, current_user rejects tokens whose sid/sub is on the Redis
    # denylist that auth-service pushes on logout/deactivation. Fail-OPEN
    # on Redis outage (ADR-0040). Same env name across the fleet; off in dev.
    session_revocation_enabled: bool = Field(
        default=False, alias="MDX_SESSION_REVOCATION_ENABLED"
    )


settings = Settings()
