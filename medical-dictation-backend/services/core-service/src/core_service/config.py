"""core-service configuration.

Per the architectural rules, this is the ONLY module permitted to read the
environment; everything else imports ``from .config import settings``.
"""

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

    # Bulk roster import (POST /patients/import). One request is one
    # transaction-per-row loop on a single connection, so the ceiling is
    # about request time and audit volume, not memory: 500 rows is a
    # clinic's whole legacy roster in a handful of uploads, and keeps the
    # worst-case request inside the ingress timeout.
    patient_import_max_rows: int = Field(
        default=500, alias="MDX_PATIENT_IMPORT_MAX_ROWS"
    )

    # ── Encounter lifecycle ─────────────────────────────────────────
    # Ending a visit is refused while a dictation session on it is still
    # live. A session stranded by a dead worker keeps a non-terminal status
    # until dictation-service's reaper clears it, so only sessions heard
    # from inside this window count — otherwise a crashed worker could wedge
    # a visit open indefinitely. Keep this comfortably above
    # dictation-service's heartbeat interval and below its reaper threshold.
    encounter_live_session_window_seconds: int = Field(
        default=120, alias="MDX_ENCOUNTER_LIVE_SESSION_WINDOW_SECONDS"
    )

    # ── Patient identity (ІПН) — S11 step 01 ────────────────────────
    # HMAC key for the ipn_hmac lookup token. Deliberately independent from
    # signing-service's SIGNER_IPN_HMAC_KEY (ADR-0027): patient identity and
    # signer identity are separate spaces; unifying them later is a config
    # change, not a schema change. Rotation orphans every stored hmac and
    # requires a re-HMAC migration under maintenance — do not rotate casually.
    patient_ipn_hmac_key: SecretStrEnv = Field(
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

    # ── Consent signing (S11 step 03) ────────────────────────────────
    signing_service_base_url: str = Field(
        default="http://localhost:8008", alias="SIGNING_SERVICE_BASE_URL"
    )
    # Approved consent texts (`<type>-<version>.md`); the default resolves
    # for repo-local dev runs, containers mount/bake the directory.
    consent_texts_dir: str = Field(
        default="infra/seeds/consents", alias="MDX_CONSENT_TEXTS_DIR"
    )

    # ── MFA gate (sprint 16) — erasure approval + DSAR export ──────────
    # Same flag name as auth-service; production sets it true for the
    # whole fleet, dev keeps it off. Enforced by deps.requires_mfa().
    require_mfa: bool = Field(default=False, alias="MDX_REQUIRE_MFA")

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
    # S11 deployment (ADR-0028): download links are short-lived HMAC
    # tokens on the authenticated decrypt-and-stream endpoint — the
    # platform's honest equivalent of "presigned at 15 minutes"
    # (raw presigned URLs serve ciphertext, rule 3). Key idiom follows
    # signing-service's *_HMAC_KEY hex fields; dev default is NOT a
    # production value.
    dsar_download_token_ttl_seconds: int = Field(
        default=900, alias="DSAR_DOWNLOAD_TOKEN_TTL_SECONDS"
    )
    dsar_download_token_hmac_key_hex: str = Field(
        default="33" * 32, alias="DSAR_DOWNLOAD_TOKEN_HMAC_KEY"
    )
    # Backups-vs-erasure completion horizon (docs/runbooks/erasure.md):
    # encrypted DB backups expire from mdx-backups after this many days
    # (bucket ILM rule), so a completed erasure is fully purged from
    # backups once one full rotation has passed. Recorded per-execution
    # in report_of_execution.backups_purged_by.
    backup_retention_days: int = Field(default=35, alias="BACKUP_RETENTION_DAYS")

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
    # 0065 — patient record attachments (referrals, lab PDFs, scans).
    s3_patient_docs_bucket: str = Field(
        default="mdx-patient-docs", alias="S3_PATIENT_DOCS_BUCKET"
    )

    # Per-file ceiling for a patient attachment. A referral letter or a lab
    # PDF is kilobytes; 25 MB leaves room for a scanned multi-page study
    # without turning the record into an image host. Enforced on the actual
    # bytes read, not on Content-Length, which a client controls.
    patient_document_max_bytes: int = Field(
        default=25 * 1024 * 1024, alias="MDX_PATIENT_DOCUMENT_MAX_BYTES"
    )
    db_audit_reader_dsn: str = Field(
        default="postgresql://audit_reader:audit_reader@localhost:5432/medical_dictation",
        alias="DB_AUDIT_READER_DSN",
    )
    # The ONLY credential that can destroy PHI rows (S11 steps 04/07) —
    # held exclusively by the erasure engine, never by request handlers.
    db_erasure_dsn: str = Field(
        default="postgresql://mdx_erasure:mdx_erasure@localhost:5432/medical_dictation",
        alias="DB_ERASURE_DSN",
    )

    # ── In-process scheduler (sprint 16, ADR-0041) ──────────────────────
    # Hosts the erasure backup-horizon notifier. Off in dev; production
    # flips MDX_BACKGROUND_JOBS. Idempotent — interval choice is free.
    background_jobs_enabled: bool = Field(default=False, alias="MDX_BACKGROUND_JOBS")
    background_jobs_interval_s: float = Field(
        default=86400.0, alias="MDX_BACKGROUND_JOBS_INTERVAL_S"
    )

    # ── Session revocation check (sprint 16) ────────────────────────────
    # When on, current_user rejects tokens whose sid/sub is on the Redis
    # denylist that auth-service pushes on logout/deactivation. Fail-OPEN
    # on Redis outage (ADR-0041). Same env name across the fleet; off in dev.
    session_revocation_enabled: bool = Field(
        default=False, alias="MDX_SESSION_REVOCATION_ENABLED"
    )
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")


settings = Settings()
