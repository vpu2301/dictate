"""dictation-service configuration. All env vars read here."""

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

    service_name: str = "dictation-service"
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

    # ── Database ────────────────────────────────────────────────────────
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
    db_pool_max_size: int = Field(default=8, alias="DB_POOL_MAX_SIZE")

    # ── Redis (rate-limit + worker liveness) ───────────────────────────
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")

    # ── MinIO / S3 (finalized audio uploads) ───────────────────────────
    s3_endpoint: str = Field(default="http://localhost:9000", alias="S3_ENDPOINT")
    s3_region: str = Field(default="us-east-1", alias="S3_REGION")
    s3_access_key: str = Field(default="minioadmin", alias="S3_ACCESS_KEY")
    s3_secret_key: str = Field(default="minioadmin", alias="S3_SECRET_KEY")
    s3_audio_bucket: str = Field(default="mdx-audio", alias="S3_AUDIO_BUCKET")
    s3_use_ssl: bool = Field(default=False, alias="S3_USE_SSL")

    # ── Master key (envelope crypto for finalized uploads) ──────────────
    master_key_path: str = Field(
        default="/etc/mdx/master.key", alias="MDX_MASTER_KEY_PATH"
    )

    # ── Streaming protocol ──────────────────────────────────────────────
    ws_subprotocol: str = Field(
        default="medical-dictation.v1", alias="MDX_WS_SUBPROTOCOL"
    )
    ws_heartbeat_interval_s: float = Field(
        default=10.0, alias="MDX_WS_HEARTBEAT_INTERVAL_S"
    )
    ws_idle_timeout_s: float = Field(
        default=35.0, alias="MDX_WS_IDLE_TIMEOUT_S"
    )
    ws_max_binary_frame_bytes: int = Field(
        default=8 * 1024, alias="MDX_WS_MAX_BINARY_FRAME_BYTES"
    )
    ws_idle_close_after_no_session_s: float = Field(
        default=10.0, alias="MDX_WS_IDLE_CLOSE_AFTER_NO_SESSION_S"
    )

    # ── Rate limits on the upgrade endpoint ─────────────────────────────
    upgrade_ratelimit_per_ip_per_minute: int = Field(
        default=10, alias="MDX_UPGRADE_RATELIMIT_PER_IP_PER_MINUTE"
    )
    upgrade_ratelimit_per_user_per_hour: int = Field(
        default=30, alias="MDX_UPGRADE_RATELIMIT_PER_USER_PER_HOUR"
    )

    # ── Session lifecycle ───────────────────────────────────────────────
    session_idle_abandon_minutes: int = Field(
        default=30, alias="MDX_SESSION_IDLE_ABANDON_MINUTES"
    )
    session_hard_cap_minutes: int = Field(
        default=60, alias="MDX_SESSION_HARD_CAP_MINUTES"
    )
    session_token_expiry_warn_seconds: int = Field(
        default=60, alias="MDX_SESSION_TOKEN_EXPIRY_WARN_SECONDS"
    )

    # ── Windowing / inference ───────────────────────────────────────────
    window_seconds: float = Field(default=4.0, alias="MDX_WINDOW_SECONDS")
    window_overlap_seconds: float = Field(
        default=2.0, alias="MDX_WINDOW_OVERLAP_SECONDS"
    )
    window_min_for_partial_seconds: float = Field(
        default=1.5, alias="MDX_WINDOW_MIN_FOR_PARTIAL_SECONDS"
    )
    window_tick_interval_ms: int = Field(
        default=600, alias="MDX_WINDOW_TICK_INTERVAL_MS"
    )
    window_inference_deadline_multiplier: float = Field(
        default=1.5, alias="MDX_WINDOW_INFERENCE_DEADLINE_MULTIPLIER"
    )
    no_speech_prob_drop_threshold: float = Field(
        default=0.6, alias="MDX_NO_SPEECH_PROB_DROP_THRESHOLD"
    )
    aligner_boundary_uncertainty_threshold: float = Field(
        default=0.30, alias="MDX_ALIGNER_BOUNDARY_UNCERTAINTY_THRESHOLD"
    )
    prompt_max_tokens: int = Field(default=150, alias="MDX_PROMPT_MAX_TOKENS")

    # ── Concurrency cap per GPU worker ──────────────────────────────────
    per_worker_max_sessions: int = Field(
        default=4, alias="MDX_PER_WORKER_MAX_SESSIONS"
    )
    per_tenant_max_active_sessions: int = Field(
        default=10, alias="MDX_PER_TENANT_MAX_ACTIVE_SESSIONS"
    )
    retransmit_max_range_frames: int = Field(
        default=1500, alias="MDX_RETRANSMIT_MAX_RANGE_FRAMES"  # 30s @ 50fps
    )

    # ── tmpfs ring buffer ───────────────────────────────────────────────
    tmpfs_root: str = Field(default="/run/dictation", alias="MDX_TMPFS_ROOT")
    # 30 min × 60 s × 16 000 Hz × 4 bytes = 115 200 000 bytes
    tmpfs_ring_seconds: int = Field(default=30 * 60, alias="MDX_TMPFS_RING_SECONDS")

    # ── Worker identity (Redis liveness key) ────────────────────────────
    worker_id: str = Field(default="worker-1", alias="MDX_WORKER_ID")
    worker_heartbeat_interval_s: float = Field(
        default=5.0, alias="MDX_WORKER_HEARTBEAT_INTERVAL_S"
    )
    worker_heartbeat_ttl_s: float = Field(
        default=30.0, alias="MDX_WORKER_HEARTBEAT_TTL_S"
    )

    # ── Origin allow-list for WS upgrades ──────────────────────────────
    ws_allowed_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://localhost:3000"],
        alias="MDX_WS_ALLOWED_ORIGINS",
    )


settings = Settings()
