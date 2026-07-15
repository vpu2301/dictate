"""core-service configuration.

Per the architectural rules, this is the ONLY module permitted to read the
environment; everything else imports ``from .config import settings``.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from secret import Secret


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env.local",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    service_name: str = "core-service"
    environment: str = Field(default="development", alias="ENVIRONMENT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    testing: bool = Field(default=False, alias="TESTING")

    otel_exporter_otlp_endpoint: str = Field(
        default="http://localhost:4317", alias="OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    otel_sdk_disabled: bool = Field(default=False, alias="OTEL_SDK_DISABLED")

    # Auth: JWKS cache + JWT validation (Keycloak)
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

    # CORS for the SPA (dev origins).
    cors_allowed_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173,http://127.0.0.1:4173",
        alias="CORS_ALLOWED_ORIGINS",
    )

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    # DB pools (separate app + audit roles, exactly like report-service).
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

    # Roster list page size ceiling.
    patient_list_max_limit: int = Field(default=200, alias="MDX_PATIENT_LIST_MAX_LIMIT")

    # ── Patient identity (ІПН) — S11 step 01 ────────────────────────
    # HMAC key for the ipn_hmac lookup token. Deliberately independent from
    # signing-service's SIGNER_IPN_HMAC_KEY (ADR-0027): patient identity and
    # signer identity are separate spaces; unifying them later is a config
    # change, not a schema change. Rotation orphans every stored hmac and
    # requires a re-HMAC migration under maintenance — do not rotate casually.
    patient_ipn_hmac_key: Secret[str] = Field(
        default_factory=lambda: Secret("00" * 32), alias="MDX_PATIENT_IPN_HMAC_KEY"
    )
    # Raw-ІПН retention (envelope-encrypted). OFF pending DPO sign-off —
    # see todo.md; the hmac path is unaffected either way.
    patient_ipn_raw_enabled: bool = Field(default=False, alias="PATIENT_IPN_RAW_ENABLED")

    # Envelope-encryption wiring, needed only when raw retention is enabled.
    db_crypto_writer_dsn: str = Field(
        default="postgresql://crypto_writer:crypto_writer@localhost:5432/medical_dictation",
        alias="DB_CRYPTO_WRITER_DSN",
    )
    master_key_path: str = Field(default="/etc/mdx/master.key", alias="MDX_MASTER_KEY_PATH")

    # ── Consent signing (S11 step 03) ────────────────────────────────
    signing_service_base_url: str = Field(
        default="http://localhost:8008", alias="SIGNING_SERVICE_BASE_URL"
    )
    # Approved consent texts (`<type>-<version>.md`); the default resolves
    # for repo-local dev runs, containers mount/bake the directory.
    consent_texts_dir: str = Field(
        default="infra/seeds/consents", alias="MDX_CONSENT_TEXTS_DIR"
    )

    # ── Erasure workflow (S11 step 04) ───────────────────────────────
    # Grace period between approval and the engine being ALLOWED to
    # execute (data-layer enforced via scheduled_for). Absorbs "the
    # patient changed their mind" — cheaper than any undelete.
    erasure_grace_days: int = Field(default=7, alias="ERASURE_GRACE_DAYS")
    # Signed clinical records inside this window are RETAINED at erasure
    # (fan-out map basis retention:clinical_record_signed). Default per
    # Ukrainian clinical-record rules; legal confirmation in todo.md.
    report_retention_years: int = Field(default=25, alias="REPORT_RETENTION_YEARS")

    # ── DSAR export engine (S11 step 06) ─────────────────────────────
    # Raw audio excluded by default; the DPO decision is a config flip.
    dsar_include_raw_audio: bool = Field(default=False, alias="DSAR_INCLUDE_RAW_AUDIO")
    # Raw ІПН never leaves the row unless the DPO enables it AND an
    # encrypted copy exists (PATIENT_IPN_RAW_ENABLED captured one).
    dsar_include_raw_ipn: bool = Field(default=False, alias="DSAR_INCLUDE_RAW_IPN")
    # Subject-accessible audit slice: the DPO's knob. Comma-separated kinds.
    dsar_audit_kinds: str = Field(
        default=(
            "patient.created,patient.updated,consent.granted,consent.withdrawn,"
            "consent.signed,privacy.dsar_requested,privacy.erasure_requested,"
            "privacy.erasure_approved,privacy.erasure_rejected"
        ),
        alias="DSAR_AUDIT_KINDS",
    )
    dsar_stale_minutes: int = Field(default=30, alias="DSAR_STALE_MINUTES")
    dsar_package_ttl_days: int = Field(default=14, alias="DSAR_PACKAGE_TTL_DAYS")

    @property
    def dsar_audit_kinds_list(self) -> list[str]:
        return [k.strip() for k in self.dsar_audit_kinds.split(",") if k.strip()]

    # Object storage + audit-read wiring for the DSAR engine (lazy-built;
    # the service runs fine without MinIO until the first export).
    s3_endpoint: str = Field(default="http://localhost:9000", alias="S3_ENDPOINT")
    s3_region: str = Field(default="us-east-1", alias="S3_REGION")
    s3_access_key: str = Field(default="minioadmin", alias="S3_ACCESS_KEY")
    s3_secret_key: str = Field(default="minioadmin", alias="S3_SECRET_KEY")
    s3_use_ssl: bool = Field(default=False, alias="S3_USE_SSL")
    s3_audio_bucket: str = Field(default="mdx-audio", alias="S3_AUDIO_BUCKET")
    s3_transcripts_bucket: str = Field(default="mdx-transcripts", alias="S3_TRANSCRIPTS_BUCKET")
    s3_dsar_bucket: str = Field(default="mdx-dsar", alias="S3_DSAR_BUCKET")
    db_audit_reader_dsn: str = Field(
        default="postgresql://audit_reader:audit_reader@localhost:5432/medical_dictation",
        alias="DB_AUDIT_READER_DSN",
    )


settings = Settings()
