"""asr-service configuration. All env vars read here (sprint-01 hook enforced)."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from secret import Secret

# ``Secret[str]`` is a generic, so pydantic-settings classes it as a complex
# type and json.loads() the raw env value — any plain string (including "")
# blows up with a JSONDecodeError before the field is ever validated. NoDecode
# hands the raw string straight to Secret's validator. Every Secret field fed
# from the environment must use this alias.
SecretStrEnv = Annotated[Secret[str], NoDecode]


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

    # ── Master-key provider (sprint 16, ADR-0011 KMS swap) ───────────────
    # 'file' (dev default — behaviour identical to pre-sprint-16) or
    # 'vault' (Vault Transit; fail-closed startup probe). With 'vault', the
    # file at master_key_path — if present — stays live as a read-only
    # fallback for rows not yet re-wrapped (scripts/kms/rewrap-tenant-keks.py).
    master_key_provider: str = Field(default="file", alias="MDX_MASTER_KEY_PROVIDER")
    vault_addr: str = Field(default="http://localhost:8200", alias="MDX_VAULT_ADDR")
    vault_token: SecretStrEnv = Field(
        default_factory=lambda: Secret(""), alias="MDX_VAULT_TOKEN"
    )
    vault_transit_key: str = Field(default="mdx-master", alias="MDX_VAULT_TRANSIT_KEY")
    vault_transit_mount: str = Field(default="transit", alias="MDX_VAULT_TRANSIT_MOUNT")

    # ── Upload validation ───────────────────────────────────────────────
    max_upload_mb: int = Field(default=100, alias="MD_ASR_MAX_UPLOAD_MB")
    max_duration_seconds: int = Field(default=30 * 60, alias="MD_ASR_MAX_DURATION_SECONDS")
    # Floor, not a cap: below this an upload cannot carry a clinical
    # utterance, and Whisper answers a fraction of a second of noise with a
    # confident hallucination. Rejecting is safer than charting it.
    min_duration_ms: int = Field(default=400, alias="MD_ASR_MIN_DURATION_MS")
    min_sample_rate_hz: int = Field(default=8000, alias="MD_ASR_MIN_SAMPLE_RATE_HZ")
    max_channels: int = Field(default=2, alias="MD_ASR_MAX_CHANNELS")
    monthly_quota_bytes: int = Field(
        default=10 * 1024 * 1024 * 1024, alias="MD_ASR_MONTHLY_QUOTA_BYTES"
    )
    ffprobe_path: str = Field(default="ffprobe", alias="MD_ASR_FFPROBE_PATH")
    ffprobe_timeout_seconds: float = Field(default=5.0, alias="MD_ASR_FFPROBE_TIMEOUT_SECONDS")

    # ── Concurrency limits ──────────────────────────────────────────────
    per_tenant_concurrent_jobs: int = Field(default=10, alias="MD_ASR_PER_TENANT_CONCURRENT_JOBS")

    # ── Stranded-job reaper ─────────────────────────────────────────────
    # The only terminal writer for a job is the worker that owns it, so a
    # worker killed mid-inference leaves its row in `running` forever —
    # burning a per_tenant_concurrent_jobs slot and showing the clinician a
    # job that never resolves. The reaper is the out-of-process backstop
    # (asr_service.domain.reaper).
    #
    # The grace windows are the ONLY interlock: asr-worker publishes no
    # heartbeat. Keep `running` comfortably above the worst case the worker
    # allows itself — max_duration_seconds × the worker's inference
    # multiplier (30 min × 5 = 2.5 h at the defaults), plus a redelivery.
    job_reaper_enabled: bool = Field(default=True, alias="MD_ASR_JOB_REAPER_ENABLED")
    job_reaper_interval_s: float = Field(
        default=300.0, alias="MD_ASR_JOB_REAPER_INTERVAL_S"
    )
    job_reaper_running_grace_s: float = Field(
        default=3 * 3600.0, alias="MD_ASR_JOB_REAPER_RUNNING_GRACE_S"
    )
    # A job nobody has claimed in this long is not backlogged, it is lost.
    job_reaper_queued_grace_s: float = Field(
        default=6 * 3600.0, alias="MD_ASR_JOB_REAPER_QUEUED_GRACE_S"
    )
    job_reaper_batch_limit: int = Field(default=100, alias="MD_ASR_JOB_REAPER_BATCH_LIMIT")

    # ── NLP batch enrichment (sprint 05 pipeline over batch results) ────
    # GET /asr/jobs/{id}/result runs the raw transcript through
    # nlp-service (voice commands → punctuation → numbers → …) before
    # returning it. Degrades gracefully to the raw transcript when the
    # service is down or the flag is off.
    nlp_enrich_enabled: bool = Field(default=True, alias="MD_ASR_NLP_ENRICH_ENABLED")
    nlp_base_url: str = Field(default="http://localhost:8005", alias="MD_ASR_NLP_BASE_URL")
    nlp_timeout_seconds: float = Field(default=10.0, alias="MD_ASR_NLP_TIMEOUT_SECONDS")

    # ── Session revocation check (sprint 16) ────────────────────────────
    # When on, current_user rejects tokens whose sid/sub is on the Redis
    # denylist that auth-service pushes on logout/deactivation. Fail-OPEN
    # on Redis outage (ADR-0040). Same env name across the fleet; off in dev.
    session_revocation_enabled: bool = Field(
        default=False, alias="MDX_SESSION_REVOCATION_ENABLED"
    )


settings = Settings()
