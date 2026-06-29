"""asr-service configuration. All env vars read here (sprint-01 hook enforced)."""

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

    service_name: str = "asr-service"
    environment: str = Field(default="development", alias="ENVIRONMENT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    testing: bool = Field(default=False, alias="TESTING")

    # OpenTelemetry
    otel_exporter_otlp_endpoint: str = Field(
        default="http://localhost:4317", alias="OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    otel_sdk_disabled: bool = Field(default=False, alias="OTEL_SDK_DISABLED")

    # ── libs/auth (Keycloak) ─────────────────────────────────────────────
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

    # ── Database DSNs ───────────────────────────────────────────────────
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

    # ── Redis Streams (libs/messaging concrete impl) ────────────────────
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    asr_jobs_stream: str = Field(default="asr:jobs", alias="MD_ASR_JOBS_STREAM")
    asr_jobs_dlq_stream: str = Field(default="asr:jobs:dlq", alias="MD_ASR_JOBS_DLQ_STREAM")
    asr_jobs_group: str = Field(default="asr-workers", alias="MD_ASR_JOBS_GROUP")
    asr_jobs_maxlen: int = Field(default=100_000, alias="MD_ASR_JOBS_MAXLEN")

    # ── MinIO / S3 ──────────────────────────────────────────────────────
    s3_endpoint: str = Field(default="http://localhost:9000", alias="S3_ENDPOINT")
    s3_region: str = Field(default="us-east-1", alias="S3_REGION")
    s3_access_key: str = Field(default="minioadmin", alias="S3_ACCESS_KEY")
    s3_secret_key: str = Field(default="minioadmin", alias="S3_SECRET_KEY")
    s3_audio_bucket: str = Field(default="mdx-audio", alias="S3_AUDIO_BUCKET")
    s3_transcripts_bucket: str = Field(default="mdx-transcripts", alias="S3_TRANSCRIPTS_BUCKET")
    s3_use_ssl: bool = Field(default=False, alias="S3_USE_SSL")
    s3_presigned_ttl_seconds: int = Field(default=300, alias="S3_PRESIGNED_TTL_SECONDS")

    # ── Master key (envelope crypto) ────────────────────────────────────
    master_key_path: str = Field(default="/etc/mdx/master.key", alias="MDX_MASTER_KEY_PATH")

    # ── Upload validation ───────────────────────────────────────────────
    max_upload_mb: int = Field(default=100, alias="MD_ASR_MAX_UPLOAD_MB")
    max_duration_seconds: int = Field(default=30 * 60, alias="MD_ASR_MAX_DURATION_SECONDS")
    min_sample_rate_hz: int = Field(default=8000, alias="MD_ASR_MIN_SAMPLE_RATE_HZ")
    max_channels: int = Field(default=2, alias="MD_ASR_MAX_CHANNELS")
    monthly_quota_bytes: int = Field(
        default=10 * 1024 * 1024 * 1024, alias="MD_ASR_MONTHLY_QUOTA_BYTES"
    )
    ffprobe_path: str = Field(default="ffprobe", alias="MD_ASR_FFPROBE_PATH")
    ffprobe_timeout_seconds: float = Field(default=5.0, alias="MD_ASR_FFPROBE_TIMEOUT_SECONDS")

    # ── Concurrency limits ──────────────────────────────────────────────
    per_tenant_concurrent_jobs: int = Field(default=10, alias="MD_ASR_PER_TENANT_CONCURRENT_JOBS")


settings = Settings()
