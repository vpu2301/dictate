"""Auth-service configuration.

All env vars read here. No ``os.environ`` access anywhere else in the
service (enforced by the sprint-01 pre-commit hook).
"""

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

    # ── MFA enforcement (sprint 02 ships disabled per pilot decision) ───
    # When MDX_REQUIRE_MFA=true, routes wrapped with the requires_mfa() dep
    # reject tokens whose ``mfa`` claim isn't True. Flipping this on is the
    # entire enablement path — no other code change is required.
    require_mfa: bool = Field(default=False, alias="MDX_REQUIRE_MFA")


settings = Settings()
