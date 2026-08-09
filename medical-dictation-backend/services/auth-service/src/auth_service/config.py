"""Auth-service configuration.

All env vars read here. No ``os.environ`` access anywhere else in the
service (enforced by the sprint-01 pre-commit hook).
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
    vault_token: Secret[str] = Field(
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

    # ── demo mode (sprint 07 HF Space) ─────────────────────────────────
    # When MDX_DEMO_MODE=true the DemoRateLimitMiddleware enforces per-IP/
    # per-user caps on session endpoints. Off everywhere but the public demo.
    demo_mode: bool = Field(default=False, alias="MDX_DEMO_MODE")


settings = Settings()
