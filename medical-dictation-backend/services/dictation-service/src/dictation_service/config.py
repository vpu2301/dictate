"""dictation-service configuration. All env vars read here."""

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

    # ── Redis (rate-limit + worker liveness + notification bus) ────────
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")

    # ── Sprint-12 notification event bus (ADR-0029) ────────────────────
    # Same kill switch report-service carries. Publishing is already
    # fire-and-forget, so this is not about failure handling — it is the
    # source-level cut-off for a notification storm (E1).
    notifications_enabled: bool = Field(
        default=True, alias="MDX_NOTIFICATIONS_ENABLED"
    )

    # ── MinIO / S3 (finalized audio uploads) ───────────────────────────
    s3_endpoint: str = Field(default="http://localhost:9000", alias="S3_ENDPOINT")
    s3_region: str = Field(default="us-east-1", alias="S3_REGION")
    s3_access_key: str = Field(default="minioadmin", alias="S3_ACCESS_KEY")
    s3_secret_key: str = Field(default="minioadmin", alias="S3_SECRET_KEY")
    s3_audio_bucket: str = Field(default="mdx-audio", alias="S3_AUDIO_BUCKET")
    s3_use_ssl: bool = Field(default=False, alias="S3_USE_SSL")

    # ── Demo privacy envelope (sprint 07, ADR-0018) ─────────────────────
    # When disabled, no finalized audio is ever written to object storage
    # (the HF Space sets this true). Purge-on-finalize additionally zeroes
    # the in-memory PCM buffer at end-of-session as defence in depth.
    object_store_disabled: bool = Field(
        default=False, alias="MD_OBJECT_STORE_DISABLED"
    )
    demo_audio_purge_on_finalize: bool = Field(
        default=False, alias="DEMO_AUDIO_PURGE_ON_FINALIZE"
    )

    # ── Master key (envelope crypto for finalized uploads) ──────────────
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

    # ── Streaming protocol ──────────────────────────────────────────────
    ws_subprotocol: str = Field(default="medical-dictation.v1", alias="MDX_WS_SUBPROTOCOL")
    ws_heartbeat_interval_s: float = Field(default=10.0, alias="MDX_WS_HEARTBEAT_INTERVAL_S")
    ws_idle_timeout_s: float = Field(default=35.0, alias="MDX_WS_IDLE_TIMEOUT_S")
    ws_max_binary_frame_bytes: int = Field(default=8 * 1024, alias="MDX_WS_MAX_BINARY_FRAME_BYTES")
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
    session_idle_abandon_minutes: int = Field(default=30, alias="MDX_SESSION_IDLE_ABANDON_MINUTES")
    session_hard_cap_minutes: int = Field(default=60, alias="MDX_SESSION_HARD_CAP_MINUTES")
    session_token_expiry_warn_seconds: int = Field(
        default=60, alias="MDX_SESSION_TOKEN_EXPIRY_WARN_SECONDS"
    )

    # ── Stale-session reaper ────────────────────────────────────────────
    # The abandon timer lives in the worker process, so a worker that dies
    # takes its timers with it and leaves every session it held stranded in
    # a non-terminal status — forever, and counting against
    # per_tenant_max_active_sessions. The reaper is the out-of-process
    # backstop: it only touches sessions whose worker's Redis heartbeat has
    # expired, so a legitimately paused session on a live worker is never
    # collected.
    session_reaper_enabled: bool = Field(default=True, alias="MDX_SESSION_REAPER_ENABLED")
    session_reaper_interval_s: float = Field(
        default=300.0, alias="MDX_SESSION_REAPER_INTERVAL_S"
    )
    # Grace after last activity before a session is even considered. Must
    # comfortably exceed worker_heartbeat_ttl_s so a rolling restart isn't
    # mistaken for a crash.
    session_reaper_grace_s: float = Field(default=300.0, alias="MDX_SESSION_REAPER_GRACE_S")
    session_reaper_batch_limit: int = Field(
        default=200, alias="MDX_SESSION_REAPER_BATCH_LIMIT"
    )

    # ── Windowing / inference ───────────────────────────────────────────
    window_seconds: float = Field(default=4.0, alias="MDX_WINDOW_SECONDS")
    window_overlap_seconds: float = Field(default=2.0, alias="MDX_WINDOW_OVERLAP_SECONDS")
    window_min_for_partial_seconds: float = Field(
        default=1.5, alias="MDX_WINDOW_MIN_FOR_PARTIAL_SECONDS"
    )
    window_tick_interval_ms: int = Field(default=600, alias="MDX_WINDOW_TICK_INTERVAL_MS")
    window_inference_deadline_multiplier: float = Field(
        default=1.5, alias="MDX_WINDOW_INFERENCE_DEADLINE_MULTIPLIER"
    )
    no_speech_prob_drop_threshold: float = Field(
        default=0.6, alias="MDX_NO_SPEECH_PROB_DROP_THRESHOLD"
    )
    # Backstop for the silence-gated commit rule: a word older than this
    # commits even without a VAD silence boundary, so continuous speech
    # can never stall the transcript (sprint-14 fix, ADR-0013 amendment).
    # 4 s = 2× the commit horizon; keeps final latency inside the
    # sprint-04 p95 ≤ 2500 ms target for the normal (silence-gated) path.
    commit_max_provisional_ms: int = Field(
        default=4000, alias="MDX_COMMIT_MAX_PROVISIONAL_MS"
    )
    aligner_boundary_uncertainty_threshold: float = Field(
        default=0.30, alias="MDX_ALIGNER_BOUNDARY_UNCERTAINTY_THRESHOLD"
    )
    prompt_max_tokens: int = Field(default=150, alias="MDX_PROMPT_MAX_TOKENS")

    # ── Conversation mode / diarization (sprint 14, ADR-0034) ───────────
    conversation_enabled: bool = Field(default=True, alias="MDX_CONVERSATION_ENABLED")
    # Baked model dir produced by scripts/models/prepare_ecapa.py
    # (Dockerfile bakes /opt/models/ecapa; macOS dev default matches the
    # prepare script's default target so `make prepare-ecapa` just works).
    diar_model_dir: str = Field(default="/opt/models/ecapa", alias="MDX_DIAR_MODEL_DIR")
    # "cpu" is the safe-everywhere default; the GPU compose overlay sets
    # MDX_DIAR_DEVICE=cuda so ECAPA shares the A10G with Whisper.
    diar_device: str = Field(default="cpu", alias="MDX_DIAR_DEVICE")
    # Build-time provenance, stamped into the image ENV by the Dockerfile's
    # ecapa-fetch stage (docs/models/PINS.md). The digests are re-asserted at
    # STARTUP, fail-closed — a mismatch refuses to serve rather than diarize
    # with unaccountable weights. Empty digests = dev path (`make
    # prepare-ecapa`): presence checked, content only logged.
    diar_model_repo: str = Field(default="", alias="MDX_DIAR_MODEL_REPO")
    diar_model_revision: str = Field(default="", alias="MDX_DIAR_MODEL_REVISION")
    diar_model_sha256: str = Field(default="", alias="MDX_DIAR_MODEL_SHA256")
    diar_meanvar_sha256: str = Field(default="", alias="MDX_DIAR_MEANVAR_SHA256")
    # Warm both models at startup rather than on the first conversation
    # session. A worker that advertises conversation capacity with a cold
    # diarizer pays ~0.7 s of load on the first window and blows the latency
    # budget; readiness gates on this instead (sprint-14 deployment).
    diar_warm_at_startup: bool = Field(default=True, alias="MDX_DIAR_WARM_AT_STARTUP")
    # A conversation session runs two models; weighted capacity below.
    # Weight 2 => 4 dictation OR 2 conversation OR 2+1 mix per worker.
    # CONFIGURED, not yet GPU-measured — see todo.md (S14) + ADR-0034.
    conversation_session_weight: int = Field(
        default=2, alias="MDX_CONVERSATION_SESSION_WEIGHT"
    )

    # ── Finalize-time NLP + draft creation (sprint 14) ──────────────────
    # Finalize is not latency-critical; generous timeouts, graceful
    # degradation (raw transcript persists if either call fails).
    nlp_base_url: str = Field(default="http://nlp-service:8000", alias="MDX_NLP_BASE_URL")
    finalize_nlp_timeout_seconds: float = Field(
        default=5.0, alias="MDX_FINALIZE_NLP_TIMEOUT_SECONDS"
    )
    report_base_url: str = Field(
        default="http://report-service:8000", alias="MDX_REPORT_BASE_URL"
    )
    report_draft_timeout_seconds: float = Field(
        default=5.0, alias="MDX_REPORT_DRAFT_TIMEOUT_SECONDS"
    )

    # ── Concurrency cap per GPU worker ──────────────────────────────────
    per_worker_max_sessions: int = Field(default=4, alias="MDX_PER_WORKER_MAX_SESSIONS")
    per_tenant_max_active_sessions: int = Field(
        default=10, alias="MDX_PER_TENANT_MAX_ACTIVE_SESSIONS"
    )
    retransmit_max_range_frames: int = Field(
        default=1500,
        alias="MDX_RETRANSMIT_MAX_RANGE_FRAMES",  # 30s @ 50fps
    )

    # ── tmpfs ring buffer ───────────────────────────────────────────────
    tmpfs_root: str = Field(default="/run/dictation", alias="MDX_TMPFS_ROOT")
    # 30 min × 60 s × 16 000 Hz × 4 bytes = 115 200 000 bytes
    tmpfs_ring_seconds: int = Field(default=30 * 60, alias="MDX_TMPFS_RING_SECONDS")

    # ── Worker identity (Redis liveness key) ────────────────────────────
    worker_id: str = Field(default="worker-1", alias="MDX_WORKER_ID")
    worker_heartbeat_interval_s: float = Field(default=5.0, alias="MDX_WORKER_HEARTBEAT_INTERVAL_S")
    worker_heartbeat_ttl_s: float = Field(default=30.0, alias="MDX_WORKER_HEARTBEAT_TTL_S")

    # ── Origin allow-list for WS upgrades ──────────────────────────────
    ws_allowed_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://localhost:3000"],
        alias="MDX_WS_ALLOWED_ORIGINS",
    )

    # ── CORS for the SPA (dev origins) ─────────────────────────────────
    # The HTTP companion surface (/healthz, /dictate/sessions/...) is called
    # cross-origin by the SPA, so it needs the same allow-list the other
    # services use. WS upgrades are gated separately by ws_allowed_origins.
    cors_allowed_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173,http://127.0.0.1:4173",
        alias="CORS_ALLOWED_ORIGINS",
    )

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    # ── Startup model warmup (sprint 16 — sprint-03 retro cold-start) ───
    # false (dev default): pre-sprint-16 blocking load — simplest, and
    # fine when warmup is fast (tiny on CPU). true (production): Whisper +
    # diarizer load in a background lifespan task; /healthz serves
    # immediately (liveness never kills a cold pod), /readyz stays 503
    # until the models are resident, so the LB sends no traffic early.
    warm_in_background: bool = Field(default=False, alias="MDX_WARM_IN_BACKGROUND")

    # ── Session revocation check (sprint 16) ────────────────────────────
    # When on, current_user rejects tokens whose sid/sub is on the Redis
    # denylist that auth-service pushes on logout/deactivation. Fail-OPEN
    # on Redis outage (ADR-0040). Same env name across the fleet; off in dev.
    session_revocation_enabled: bool = Field(
        default=False, alias="MDX_SESSION_REVOCATION_ENABLED"
    )


settings = Settings()
