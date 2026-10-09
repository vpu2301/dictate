"""Auth-service configuration.

All env vars read here. No ``os.environ`` access anywhere else in the
service (enforced by the sprint-01 pre-commit hook).
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

    service_name: str = "auth-service"
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

    # ── Database DSNs ───────────────────────────────────────────────────
    # Each role's pool is constructed at app startup. RLS depends on running
    # as the *right* role — never mix the DSNs.
    db_app_role_dsn: str = Field(
        default="postgresql://app_role:app_role@localhost:5432/medical_dictation",
        alias="DB_APP_ROLE_DSN",
    )
    db_tenant_writer_dsn: str = Field(
        default="postgresql://tenant_writer:tenant_writer@localhost:5432/medical_dictation",
        alias="DB_TENANT_WRITER_DSN",
    )
    db_audit_writer_dsn: str = Field(
        default="postgresql://audit_writer:audit_writer@localhost:5432/medical_dictation",
        alias="DB_AUDIT_WRITER_DSN",
    )
    db_audit_reader_dsn: str = Field(
        default="postgresql://audit_reader:audit_reader@localhost:5432/medical_dictation",
        alias="DB_AUDIT_READER_DSN",
    )

    db_pool_min_size: int = Field(default=1, alias="DB_POOL_MIN_SIZE")
    db_pool_max_size: int = Field(default=10, alias="DB_POOL_MAX_SIZE")

    # ── Keycloak (server-side login proxy + admin API) ──────────────────
    keycloak_base_url: str = Field(default="http://localhost:8088", alias="KEYCLOAK_BASE_URL")
    keycloak_realm: str = Field(default="medical-dictation", alias="KEYCLOAK_REALM")
    keycloak_login_client_id: str = Field(default="mdx-backend", alias="KEYCLOAK_LOGIN_CLIENT_ID")
    keycloak_login_client_secret: str = Field(
        default="dev-secret-change-in-prod-mdx-backend",
        alias="KEYCLOAK_LOGIN_CLIENT_SECRET",
    )
    keycloak_admin_client_id: str = Field(default="mdx-admin", alias="KEYCLOAK_ADMIN_CLIENT_ID")
    keycloak_admin_client_secret: str = Field(
        default="dev-secret-change-in-prod-mdx-admin",
        alias="KEYCLOAK_ADMIN_CLIENT_SECRET",
    )

    # ── Refresh cookie ──────────────────────────────────────────────────
    auth_cookie_name: str = Field(default="mdx_rt", alias="AUTH_COOKIE_NAME")
    auth_cookie_path: str = Field(default="/auth", alias="AUTH_COOKIE_PATH")
    # In dev (http://localhost) browsers won't set a Secure cookie. Default
    # off in development; staging/prod environments must override.
    auth_cookie_secure: bool = Field(default=False, alias="AUTH_COOKIE_SECURE")
    # SameSite for the refresh cookie. The dev SPA (http://localhost:5173) and
    # auth-service (http://localhost:8000) are same-site (both localhost) but
    # cross-origin; `lax` is sent on those XHR/fetch calls and is the safe SPA
    # default. Cross-SITE prod deployments must set `none` + Secure.
    auth_cookie_samesite: str = Field(default="lax", alias="AUTH_COOKIE_SAMESITE")

    # ── Step-up re-authentication (S14 break-glass) ─────────────────────
    # How long a minted reauth ticket stays redeemable. Long enough to
    # finish typing a justification, short enough that a ticket left in a
    # tab is worthless by the time anyone finds it.
    reauth_ticket_ttl_seconds: int = Field(default=300, alias="MDX_REAUTH_TICKET_TTL_SECONDS")

    # ── CORS (sprint A3 — SPA integration) ──────────────────────────────
    # Comma-separated browser origins allowed to call this service WITH
    # credentials (the HttpOnly refresh cookie). Must be explicit origins —
    # never "*" — because allow_credentials=true forbids the wildcard.
    cors_allowed_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173,http://127.0.0.1:4173",
        alias="CORS_ALLOWED_ORIGINS",
    )

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    # ── MFA enforcement (sprint 16 pays the sprint-02 IOU) ──────────────
    # When MDX_REQUIRE_MFA=true, routes wrapped with the requires_mfa() dep
    # reject tokens whose ``mfa`` claim isn't True, with the grace flow:
    # an unenrolled user gets 403 `mfa_enrolment_required` (the FE routes
    # to enrolment), an enrolled user with a pre-enrolment token gets 401.
    # Production default is true (deploy config); dev keeps it off.
    require_mfa: bool = Field(default=False, alias="MDX_REQUIRE_MFA")
    # TOTP enrolment surface (sprint 16). Enrolment endpoints live behind
    # their own switch so ops can stage the rollout: enable enrolment
    # first, let users enrol, then flip MDX_REQUIRE_MFA. Off in dev by
    # default — flipping it on requires the envelope wiring below (the
    # TOTP secret is stored envelope-encrypted in Keycloak attributes).
    mfa_enrolment_enabled: bool = Field(default=False, alias="MDX_MFA_ENROLMENT_ENABLED")
    mfa_totp_issuer: str = Field(default="Medical Dictation", alias="MDX_MFA_TOTP_ISSUER")

    # Envelope wiring for the TOTP secret store (lazy-built on first MFA
    # call; the service runs fine without the master key until then).
    db_crypto_writer_dsn: str = Field(
        default="postgresql://crypto_writer:crypto_writer@localhost:5432/medical_dictation",
        alias="DB_CRYPTO_WRITER_DSN",
    )
    master_key_path: str = Field(default="/etc/mdx/master.key", alias="MDX_MASTER_KEY_PATH")

    # ── Master-key provider (sprint 16, ADR-0011 KMS swap) ───────────────
    master_key_provider: str = Field(default="file", alias="MDX_MASTER_KEY_PROVIDER")
    vault_addr: str = Field(default="http://localhost:8200", alias="MDX_VAULT_ADDR")
    vault_token: SecretStrEnv = Field(
        default_factory=lambda: Secret(""), alias="MDX_VAULT_TOKEN"
    )
    vault_transit_key: str = Field(default="mdx-master", alias="MDX_VAULT_TRANSIT_KEY")
    vault_transit_mount: str = Field(default="transit", alias="MDX_VAULT_TRANSIT_MOUNT")

    # ── Session revocation (sprint 16 — closes the 15-min window) ───────
    # When on: logout / refresh-replay / deactivation push the session's
    # `sid` (or the user's `sub`) onto a Redis denylist checked by
    # current_user across the fleet (each service has its own flag; same
    # env name everywhere). Fail-OPEN on Redis outage (ADR-0040) — the
    # degraded mode is exactly the pre-sprint-16 posture. Default off.
    session_revocation_enabled: bool = Field(
        default=False, alias="MDX_SESSION_REVOCATION_ENABLED"
    )
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    # TTL for sub-level denies (deactivation, refresh replay): must cover
    # the access-token lifetime (15 min in the realm) with margin.
    revoked_sub_ttl_seconds: int = Field(
        default=1200, alias="MDX_REVOKED_SUB_TTL_SECONDS"
    )

    # ── Notification bus (S21 — the MFA reminder producer) ─────────────
    # Same kill switch and same env name as every other producer in the
    # estate (ADR-0029). Off ⇒ the reminder is still RECORDED and still
    # renders as a banner; only the bell/email half goes quiet.
    notifications_enabled: bool = Field(default=True, alias="MDX_NOTIFICATIONS_ENABLED")

    # ── demo mode (sprint 07 HF Space) ─────────────────────────────────
    # When MDX_DEMO_MODE=true the DemoRateLimitMiddleware enforces per-IP/
    # per-user caps on session endpoints. Off everywhere but the public demo.
    demo_mode: bool = Field(default=False, alias="MDX_DEMO_MODE")

    # ── Password recovery ───────────────────────────────────────────────
    # The forgot/reset surface. Off by default like every other feature
    # switch here: it mints credentials over email, so a deployment that
    # has not configured a mail relay must not advertise the endpoint.
    password_reset_enabled: bool = Field(
        default=False, alias="MDX_PASSWORD_RESET_ENABLED"
    )
    # 30 minutes. OWASP puts the useful range at 15–60: long enough to
    # survive a slow mail relay and a user who reads mail on their phone
    # and resets on a desktop, short enough that a link sitting in an
    # unattended inbox stops being a key fairly quickly.
    password_reset_ttl_seconds: int = Field(
        default=1800, alias="MDX_PASSWORD_RESET_TTL_SECONDS"
    )
    # The lockdown link lives much longer than the reset link — 7 days.
    # It rides in the "your password was changed" mail, and the whole
    # point of that mail is the case where the change was NOT the account
    # holder: they may be asleep, on shift, or away for the weekend when
    # it lands. A 30-minute panic button that has already expired by the
    # time somebody reads the mail is not a panic button.
    lockdown_token_ttl_seconds: int = Field(
        default=604800, alias="MDX_LOCKDOWN_TOKEN_TTL_SECONDS"
    )
    # Minimum length. NIST SP 800-63B §5.1.1.2: length is the control that
    # matters, composition rules are not, and 8 is the floor for
    # memorised secrets — 12 is the floor for anything guarding PHI.
    password_min_length: int = Field(default=12, alias="MDX_PASSWORD_MIN_LENGTH")

    # Abuse caps for the unauthenticated request endpoint. Two windows:
    # per-IP stops a broad enumeration sweep, per-email stops one mailbox
    # being flooded by a stranger. Both fail OPEN on a Redis outage —
    # house pattern, and the alternative is locking everyone out of
    # account recovery because a cache is down.
    password_reset_ip_per_hour: int = Field(
        default=20, alias="MDX_PASSWORD_RESET_IP_PER_HOUR"
    )
    password_reset_email_per_hour: int = Field(
        default=5, alias="MDX_PASSWORD_RESET_EMAIL_PER_HOUR"
    )
    # Salt for the stored IP hashes. MUST be set per-deployment: the
    # address space is small enough to brute-force an unsalted hash back
    # to the original IP in seconds.
    password_reset_ip_hash_salt: SecretStrEnv = Field(
        default_factory=lambda: Secret("dev-ip-hash-salt-change-in-prod"),
        alias="MDX_PASSWORD_RESET_IP_HASH_SALT",
    )

    # ── Outbound mail (password recovery) ───────────────────────────────
    # `mock` captures in memory and refuses to run in production; `smtp`
    # talks to Mailpit in dev and Google Workspace in prod.
    email_provider: str = Field(default="mock", alias="MDX_EMAIL_PROVIDER")
    auth_smtp_host: str = Field(default="localhost", alias="MDX_AUTH_SMTP_HOST")
    auth_smtp_port: int = Field(default=1025, alias="MDX_AUTH_SMTP_PORT")
    auth_smtp_use_tls: bool = Field(default=False, alias="MDX_AUTH_SMTP_USE_TLS")
    auth_smtp_username: str = Field(default="", alias="MDX_AUTH_SMTP_USERNAME")
    # Google Workspace has refused plain account passwords for SMTP since
    # 2024 — this must be a 16-character App Password (the account needs
    # 2-Step Verification on). The symptom of getting it wrong is
    # `535-5.7.8 Username and Password not accepted`.
    auth_smtp_password: SecretStrEnv = Field(
        default_factory=lambda: Secret(""), alias="MDX_AUTH_SMTP_PASSWORD"
    )
    auth_email_from: str = Field(
        default="sales@klarnote.com", alias="MDX_AUTH_EMAIL_FROM"
    )
    auth_email_from_name: str = Field(
        default="Klarnote", alias="MDX_AUTH_EMAIL_FROM_NAME"
    )
    auth_email_reply_to: str = Field(
        default="sales@klarnote.com", alias="MDX_AUTH_EMAIL_REPLY_TO"
    )
    # SPA origin the mailed links point at. The reset link is only useful
    # if it lands on the app the user actually runs.
    app_base_url: str = Field(default="http://localhost:5173", alias="MDX_APP_BASE_URL")
    support_url: str = Field(
        default="https://klarnote.com/contact", alias="MDX_SUPPORT_URL"
    )

    # ── Outbox delivery worker ──────────────────────────────────────────
    background_jobs_enabled: bool = Field(default=True, alias="MDX_BACKGROUND_JOBS")
    mail_delivery_interval_s: float = Field(
        default=5.0, alias="MDX_AUTH_MAIL_DELIVERY_INTERVAL_S"
    )
    mail_delivery_batch_size: int = Field(
        default=25, alias="MDX_AUTH_MAIL_DELIVERY_BATCH"
    )
    mail_delivery_max_attempts: int = Field(
        default=5, alias="MDX_AUTH_MAIL_DELIVERY_MAX_ATTEMPTS"
    )
    mail_delivery_backoff_base_s: float = Field(
        default=60.0, alias="MDX_AUTH_MAIL_DELIVERY_BACKOFF_BASE_S"
    )

    @property
    def is_production(self) -> bool:
        return self.environment in {"production", "staging"}


settings = Settings()
