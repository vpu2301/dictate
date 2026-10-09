"""autocomplete-service configuration."""

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

    service_name: str = "autocomplete-service"
    environment: str = Field(default="development", alias="ENVIRONMENT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    testing: bool = Field(default=False, alias="TESTING")

    otel_exporter_otlp_endpoint: str = Field(
        default="http://localhost:4317", alias="OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    otel_sdk_disabled: bool = Field(default=False, alias="OTEL_SDK_DISABLED")

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

    db_app_role_dsn: str = Field(
        default="postgresql://app_role:app_role@localhost:5432/medical_dictation",
        alias="DB_APP_ROLE_DSN",
    )
    db_audit_writer_dsn: str = Field(
        default="postgresql://audit_writer:audit_writer@localhost:5432/medical_dictation",
        alias="DB_AUDIT_WRITER_DSN",
    )
    db_pool_min_size: int = Field(default=2, alias="DB_POOL_MIN_SIZE")
    db_pool_max_size: int = Field(default=16, alias="DB_POOL_MAX_SIZE")
    db_statement_cache_size: int = Field(default=0, alias="DB_STATEMENT_CACHE_SIZE")

    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    trie_cache_ttl_seconds: int = Field(default=3600, alias="MDX_TRIE_CACHE_TTL")

    suggest_default_limit: int = Field(default=3, alias="MDX_SUGGEST_DEFAULT_LIMIT")
    suggest_max_limit: int = Field(default=10, alias="MDX_SUGGEST_MAX_LIMIT")

    phrase_max_creates_per_hour: int = Field(default=100, alias="MDX_PHRASE_MAX_PER_HOUR")

    # ── eval scoring (migration 0091) ───────────────────────────────────
    # Where the WER runner sends eval audio. It is asr-service's ordinary
    # batch path with the caller's own bearer forwarded — same model, same
    # prompts, same NLP pass a clinician's dictation gets, because a WER
    # measured against a different pipeline measures a different product.
    asr_service_base_url: str = Field(
        default="http://asr-service:8000", alias="ASR_SERVICE_BASE_URL"
    )
    asr_request_timeout_seconds: float = Field(
        default=20.0, alias="MDX_EVAL_ASR_TIMEOUT_S"
    )
    # How many eval utterances may sit in asr-service at once. Kept below
    # the per-tenant concurrent-job cap so an eval run cannot monopolise the
    # queue a clinician's real dictation is waiting in.
    eval_max_in_flight: int = Field(default=2, alias="MDX_EVAL_MAX_IN_FLIGHT")
    # A claimed utterance whose submit never landed (closed tab mid-upload)
    # goes back in the queue after this long. Only ever claims with no job
    # id — real transcription is never interrupted, however slow the rig.
    eval_claim_stale_seconds: int = Field(default=120, alias="MDX_EVAL_CLAIM_STALE_S")
    telemetry_flush_interval_s: float = Field(default=5.0, alias="MDX_TELEMETRY_FLUSH_S")
    telemetry_flush_batch: int = Field(default=100, alias="MDX_TELEMETRY_FLUSH_BATCH")

    # In-process maintenance loops (partition rotation + nightly roll-up).
    # Disable when an external scheduler (cron/k8s CronJob) owns these jobs.
    background_jobs_enabled: bool = Field(default=True, alias="MDX_BACKGROUND_JOBS")
    background_jobs_interval_s: float = Field(
        default=86400.0, alias="MDX_BACKGROUND_JOBS_INTERVAL_S"
    )

    # ── Telemetry cold-archive (sprint 16 — pays the sprint-10 IOU) ────
    # When on, partition rotation ARCHIVES a >90-day telemetry partition
    # to encrypted object storage BEFORE dropping it; an archive failure
    # blocks the drop (retention becomes non-destructive). Off in dev —
    # the pre-sprint-16 destructive drop stays the default until ops
    # provisions the bucket + envelope wiring below.
    telemetry_cold_archive_enabled: bool = Field(
        default=False, alias="MDX_TELEMETRY_COLD_ARCHIVE_ENABLED"
    )
    s3_endpoint: str = Field(default="http://localhost:9000", alias="S3_ENDPOINT")
    s3_region: str = Field(default="us-east-1", alias="S3_REGION")
    s3_access_key: str = Field(default="minioadmin", alias="S3_ACCESS_KEY")
    s3_secret_key: str = Field(default="minioadmin", alias="S3_SECRET_KEY")
    s3_use_ssl: bool = Field(default=False, alias="S3_USE_SSL")
    s3_telemetry_archive_bucket: str = Field(
        default="mdx-telemetry-archive", alias="S3_TELEMETRY_ARCHIVE_BUCKET"
    )
    # Envelope wiring (archives are encrypted at rest, rule 3/4).
    db_crypto_writer_dsn: str = Field(
        default="postgresql://crypto_writer:crypto_writer@localhost:5432/medical_dictation",
        alias="DB_CRYPTO_WRITER_DSN",
    )
    master_key_path: str = Field(default="/etc/mdx/master.key", alias="MDX_MASTER_KEY_PATH")
    master_key_provider: str = Field(default="file", alias="MDX_MASTER_KEY_PROVIDER")
    vault_addr: str = Field(default="http://localhost:8200", alias="MDX_VAULT_ADDR")
    vault_token: SecretStrEnv = Field(
        default_factory=lambda: Secret(""), alias="MDX_VAULT_TOKEN"
    )
    vault_transit_key: str = Field(default="mdx-master", alias="MDX_VAULT_TRANSIT_KEY")
    vault_transit_mount: str = Field(default="transit", alias="MDX_VAULT_TRANSIT_MOUNT")

    # ── Session revocation check (sprint 16) ────────────────────────────
    # When on, current_user rejects tokens whose sid/sub is on the Redis
    # denylist that auth-service pushes on logout/deactivation. Fail-OPEN
    # on Redis outage (ADR-0040). Same env name across the fleet; off in dev.
    session_revocation_enabled: bool = Field(
        default=False, alias="MDX_SESSION_REVOCATION_ENABLED"
    )


settings = Settings()
