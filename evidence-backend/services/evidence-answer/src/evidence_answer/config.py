"""Config — the sole env surface of evidence-answer (rule E8)."""

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

    service_name: str = "evidence-answer"
    environment: str = Field(default="development", alias="ENVIRONMENT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    testing: bool = Field(default=False, alias="TESTING")
    otel_exporter_otlp_endpoint: str = Field(
        default="http://localhost:4317", alias="OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    otel_sdk_disabled: bool = Field(default=False, alias="OTEL_SDK_DISABLED")

    # Recorded in every answer_provenance row (rule DP3).
    build_version: str = Field(default="dev", alias="BUILD_VERSION")
    # Bumped whenever a prompt or a pipeline stage changes (rule LM3).
    pipeline_version: str = "quick-search-1.0"

    # Auth (inherited platform IdP, rule I1)
    auth_issuer: str = Field(
        default="http://localhost:8088/realms/medical-dictation", alias="AUTH_ISSUER"
    )
    auth_jwks_url: str = Field(
        default="http://localhost:8088/realms/medical-dictation/protocol/openid-connect/certs",
        alias="AUTH_JWKS_URL",
    )
    auth_audience: str = Field(default="mdx-api", alias="AUTH_AUDIENCE")
    auth_clock_skew_seconds: int = Field(default=30, alias="AUTH_CLOCK_SKEW_SECONDS")

    db_app_role_dsn: str = Field(
        default="postgresql://app_role:app_role@localhost:5432/medical_dictation",
        alias="DB_APP_ROLE_DSN",
    )
    db_audit_writer_dsn: str = Field(
        default="postgresql://audit_writer:audit_writer@localhost:5432/medical_dictation",
        alias="DB_AUDIT_WRITER_DSN",
    )
    db_crypto_writer_dsn: str = Field(
        default="postgresql://crypto_writer:crypto_writer@localhost:5432/medical_dictation",
        alias="DB_CRYPTO_WRITER_DSN",
    )
    db_pool_min_size: int = Field(default=1, alias="DB_POOL_MIN_SIZE")
    db_pool_max_size: int = Field(default=10, alias="DB_POOL_MAX_SIZE")

    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")

    # Answer envelopes go through EncryptedObjectStore (rule E3): a question
    # may carry incidental PHI, and the envelope echoes the question.
    s3_endpoint: str = Field(default="http://localhost:9000", alias="S3_ENDPOINT")
    s3_region: str = Field(default="us-east-1", alias="S3_REGION")
    s3_access_key: str = Field(default="minioadmin", alias="S3_ACCESS_KEY")
    s3_secret_key: str = Field(default="minioadmin", alias="S3_SECRET_KEY")
    s3_use_ssl: bool = Field(default=False, alias="S3_USE_SSL")
    s3_answers_bucket: str = Field(default="eva-corpus", alias="S3_ANSWERS_BUCKET")
    master_key_path: str = Field(default="/etc/mdx/master.key", alias="MDX_MASTER_KEY_PATH")

    gateway_base_url: str = Field(default="http://localhost:8015", alias="EVA_GATEWAY_BASE_URL")
    gateway_service_token: str | None = Field(default=None, alias="EVA_GATEWAY_SERVICE_TOKEN")

    retrieval_base_url: str = Field(default="http://localhost:8011", alias="EVA_RETRIEVAL_BASE_URL")
    retrieval_service_token: str | None = Field(default=None, alias="EVA_RETRIEVAL_SERVICE_TOKEN")

    # ── CORS ────────────────────────────────────────────────────────────
    # Unlike /retrieve, this service IS browser-facing: the dictat evidence
    # module calls POST /answers directly in dev. Explicit origins only —
    # allow_credentials forbids the wildcard.
    cors_allowed_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173", alias="CORS_ALLOWED_ORIGINS"
    )

    # ── pipeline budgets ────────────────────────────────────────────────
    # Two answers in flight per user (spec NFR).
    max_inflight_per_user: int = Field(default=2, alias="EVA_ANSWER_MAX_INFLIGHT")
    inflight_ttl_seconds: int = Field(default=180, alias="EVA_ANSWER_INFLIGHT_TTL_S")
    corpus_k: int = Field(default=8, alias="EVA_ANSWER_CORPUS_K")
    web_k: int = Field(default=6, alias="EVA_ANSWER_WEB_K")
    max_evidence_blocks: int = Field(default=12, alias="EVA_ANSWER_MAX_BLOCKS")
    synthesis_max_tokens: int = Field(default=1200, alias="EVA_ANSWER_SYNTHESIS_TOKENS")
    # LM2 ceiling; the gateway clamps again, so this is belt and braces.
    synthesis_temperature: float = Field(
        default=0.2, ge=0.0, le=0.2, alias="EVA_ANSWER_TEMPERATURE"
    )
    corpus_timeout_s: float = Field(default=20.0, alias="EVA_ANSWER_CORPUS_TIMEOUT_S")
    web_timeout_s: float = Field(default=30.0, alias="EVA_ANSWER_WEB_TIMEOUT_S")
    heartbeat_seconds: float = Field(default=10.0, alias="EVA_ANSWER_HEARTBEAT_S")

    # ── feature flags (rule DP1: internal until the mode is signed off) ──
    web_enabled: bool = Field(default=True, alias="EVA_ANSWER_WEB_ENABLED")
    triage_classifier_enabled: bool = Field(default=True, alias="EVA_ANSWER_TRIAGE_CLASSIFIER")

    suggestions_limit: int = Field(default=8, alias="EVA_ANSWER_SUGGESTIONS_LIMIT")
    history_page_size: int = Field(default=20, alias="EVA_ANSWER_HISTORY_PAGE_SIZE")

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def is_development(self) -> bool:
        return self.environment == "development"


settings = Settings()
