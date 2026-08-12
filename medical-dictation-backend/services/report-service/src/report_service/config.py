"""report-service configuration."""

from __future__ import annotations

from typing import Annotated, Literal

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

    service_name: str = "report-service"
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
    db_pool_min_size: int = Field(default=1, alias="DB_POOL_MIN_SIZE")
    db_pool_max_size: int = Field(default=8, alias="DB_POOL_MAX_SIZE")

    # Sprint-12 notification event bus (ADR-0029).
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    # Set false to stop emitting entirely — the escape hatch if a
    # notification storm ever needs to be cut off at the source (E1).
    notifications_enabled: bool = Field(default=True, alias="MDX_NOTIFICATIONS_ENABLED")

    # In-process TTLCache for templates
    template_cache_maxsize: int = Field(default=5000, alias="MDX_TEMPLATE_CACHE_MAXSIZE")
    template_cache_ttl_seconds: int = Field(default=60, alias="MDX_TEMPLATE_CACHE_TTL_SECONDS")

    # ── Break-glass PHI access (S14) ───────────────────────────────
    # How long one break-glass grant stays valid. Long enough to read a
    # report and export it; short enough that an admin who requested
    # access last Tuesday cannot still open it today. Re-requesting is
    # cheap (and leaves a second audit trail), so err short.
    phi_access_grant_ttl_minutes: int = Field(
        default=60, alias="MDX_PHI_ACCESS_GRANT_TTL_MINUTES"
    )

    # Issuing organisation printed on the unsigned PDF (M1·A3).
    pdf_issuer_name: str = Field(default="Medical Dictation", alias="MDX_PDF_ISSUER_NAME")

    # signing-service base URL — the report sign surface (S09-rev)
    # delegates envelope creation there, forwarding the caller's JWT.
    signing_service_base_url: str = Field(
        default="http://localhost:8008", alias="SIGNING_SERVICE_BASE_URL"
    )

    # Sprint 13: a required structured_diagnosis section carrying only
    # extractor PROPOSALS blocks finalize until the clinician confirms.
    # Defaults strict. NOTE: platform-wide, not per-tenant — the repo has
    # no tenant-settings mechanism yet (see todo.md); when one lands, this
    # becomes its default and the validator already takes the value as an
    # argument.
    require_confirmed_diagnosis_on_finalize: bool = Field(
        default=True, alias="MDX_REQUIRE_CONFIRMED_DIAGNOSIS_ON_FINALIZE"
    )

    # Sprint 13: typed-field extraction at draft assembly (ADR-0028).
    # Fail-open — an unreachable nlp-service costs proposals, not drafts.
    nlp_service_base_url: str = Field(
        default="http://localhost:8005", alias="MDX_NLP_SERVICE_BASE_URL"
    )

    # asr-service base URL — "assign transcription to patient" fetches the
    # completed job's transcript from there, forwarding the caller's JWT.
    asr_service_base_url: str = Field(default="http://localhost:8001", alias="ASR_SERVICE_BASE_URL")

    # ── Report synthesis (spec item 1) ──────────────────────────────────
    # "mock" (default) is the deterministic offline engine — no external
    # LLM, no PHI leaving the box. "anthropic" wires the production stub
    # (Claude Opus 4.x, model id below); enabling it requires implementing
    # the real client AND a compliance sign-off.
    synthesis_provider: Literal["mock", "anthropic"] = Field(
        default="mock", alias="MDX_SYNTHESIS_PROVIDER"
    )
    synthesis_model: str = Field(default="claude-opus-4-8", alias="MDX_SYNTHESIS_MODEL")

    # ── Sprint 15: audio replay (ADR-0037) ──────────────────────────────
    # Clip creation decrypts session/batch audio, slices, re-encodes and
    # serves it from an authenticated stream — report-service therefore
    # gets the same S3+crypto wiring dictation-service has (same env
    # names, so compose blocks are copy-paste).
    db_crypto_writer_dsn: str = Field(
        default="postgresql://crypto_writer:crypto_writer@localhost:5432/medical_dictation",
        alias="DB_CRYPTO_WRITER_DSN",
    )
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
    s3_endpoint: str = Field(default="http://localhost:9000", alias="S3_ENDPOINT")
    s3_region: str = Field(default="us-east-1", alias="S3_REGION")
    s3_access_key: str = Field(default="minioadmin", alias="S3_ACCESS_KEY")
    s3_secret_key: str = Field(default="minioadmin", alias="S3_SECRET_KEY")
    s3_use_ssl: bool = Field(default=False, alias="S3_USE_SSL")
    s3_audio_bucket: str = Field(default="mdx-audio", alias="S3_AUDIO_BUCKET")
    s3_transcripts_bucket: str = Field(
        default="mdx-transcripts", alias="S3_TRANSCRIPTS_BUCKET"
    )
    # Ephemeral clip derivatives: 1-day bucket ILM backstop; the REAL
    # lifetime is the 5-minute Redis registry + token TTL below.
    s3_clips_bucket: str = Field(default="mdx-audio-clips", alias="S3_CLIPS_BUCKET")
    object_store_disabled: bool = Field(default=False, alias="MD_OBJECT_STORE_DISABLED")

    # HMAC key for clip download tokens (DSAR download-token idiom,
    # ADR-0028): hex-encoded, dev default is NOT a secret. Rotate freely —
    # tokens live 5 minutes.
    clip_token_hmac_key_hex: str = Field(
        default="6d64782d6465762d636c69702d746f6b656e2d6b65792d3030303030303030",
        alias="MDX_CLIP_TOKEN_HMAC_KEY_HEX",
    )
    clip_token_ttl_seconds: int = Field(default=300, alias="MDX_CLIP_TOKEN_TTL_SECONDS")
    clip_max_span_ms: int = Field(default=60_000, alias="MDX_CLIP_MAX_SPAN_MS")
    clip_pad_ms: int = Field(default=300, alias="MDX_CLIP_PAD_MS")
    clips_per_user_per_hour: int = Field(default=30, alias="MDX_CLIPS_PER_USER_PER_HOUR")
    ffmpeg_path: str = Field(default="ffmpeg", alias="MDX_FFMPEG_PATH")

    # ── In-process scheduler (sprint 16, ADR-0041) ──────────────────────
    # Hosts the idle-draft cleanup loop. Off in dev (run the CLI when you
    # need it); production flips MDX_BACKGROUND_JOBS. Interval default =
    # daily; the job is idempotent, so shorter intervals are safe.
    background_jobs_enabled: bool = Field(default=False, alias="MDX_BACKGROUND_JOBS")
    background_jobs_interval_s: float = Field(
        default=86400.0, alias="MDX_BACKGROUND_JOBS_INTERVAL_S"
    )
    # Draft-idleness threshold (spec §4.4 / docs/runbooks/reports.md: 30d).
    idle_draft_days: int = Field(default=30, alias="MDX_IDLE_DRAFT_DAYS")

    # ── Session revocation check (sprint 16) ────────────────────────────
    # When on, current_user rejects tokens whose sid/sub is on the Redis
    # denylist that auth-service pushes on logout/deactivation. Fail-OPEN
    # on Redis outage (ADR-0041). Same env name across the fleet; off in dev.
    session_revocation_enabled: bool = Field(
        default=False, alias="MDX_SESSION_REVOCATION_ENABLED"
    )


settings = Settings()
