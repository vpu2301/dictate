"""nlp-service configuration."""

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

    service_name: str = "nlp-service"
    environment: str = Field(default="development", alias="ENVIRONMENT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    testing: bool = Field(default=False, alias="TESTING")

    otel_exporter_otlp_endpoint: str = Field(
        default="http://localhost:4317", alias="OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    otel_sdk_disabled: bool = Field(default=False, alias="OTEL_SDK_DISABLED")

    # ── Auth (Keycloak) ─────────────────────────────────────────────────
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

    # ── Database ────────────────────────────────────────────────────────
    db_app_role_dsn: str = Field(
        default="postgresql://app_role:app_role@localhost:5432/medical_dictation",
        alias="DB_APP_ROLE_DSN",
    )
    db_audit_writer_dsn: str = Field(
        default="postgresql://audit_writer:audit_writer@localhost:5432/medical_dictation",
        alias="DB_AUDIT_WRITER_DSN",
    )
    db_pool_min_size: int = Field(default=1, alias="DB_POOL_MIN_SIZE")
    db_pool_max_size: int = Field(default=8, alias="DB_POOL_MAX_SIZE")

    # ── Redis cache (idempotence) ──────────────────────────────────────
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    cache_ttl_seconds: int = Field(default=3600, alias="MDX_NLP_CACHE_TTL_SECONDS")
    cache_key_prefix: str = Field(
        default="mdx:nlp:cache", alias="MDX_NLP_CACHE_KEY_PREFIX"
    )

    # ── Input limits ────────────────────────────────────────────────────
    max_input_bytes: int = Field(default=8 * 1024, alias="MDX_NLP_MAX_INPUT_BYTES")
    max_input_words: int = Field(default=1000, alias="MDX_NLP_MAX_INPUT_WORDS")

    # ── Rate limits ────────────────────────────────────────────────────
    rate_limit_per_tenant_rps: int = Field(
        default=1000, alias="MDX_NLP_RATE_LIMIT_PER_TENANT_RPS"
    )
    rate_limit_per_ip_rps: int = Field(
        default=50, alias="MDX_NLP_RATE_LIMIT_PER_IP_RPS"
    )

    # ── Stage 2: punctuation ───────────────────────────────────────────
    punctuation_model: str = Field(
        default="oliverguhr/fullstop-punctuation-multilang-large",
        alias="MDX_NLP_PUNCTUATION_MODEL",
    )
    punctuation_timeout_ms: int = Field(
        default=250, alias="MDX_NLP_PUNCTUATION_TIMEOUT_MS"
    )
    punctuation_token_budget: int = Field(
        default=256, alias="MDX_NLP_PUNCTUATION_TOKEN_BUDGET"
    )
    punctuation_disabled: bool = Field(
        default=False, alias="MDX_NLP_PUNCTUATION_DISABLED"
    )

    # ── Abbreviation snapshot cache ────────────────────────────────────
    abbreviation_snapshot_cache_ttl_seconds: float = Field(
        default=60.0, alias="MDX_NLP_ABBREV_CACHE_TTL_SECONDS"
    )

    # ── Confidence thresholds ──────────────────────────────────────────
    confidence_high_concern_below: float = Field(
        default=0.40, alias="MDX_NLP_CONFIDENCE_HIGH_CONCERN_BELOW"
    )
    confidence_moderate_below: float = Field(
        default=0.65, alias="MDX_NLP_CONFIDENCE_MODERATE_BELOW"
    )


settings = Settings()
