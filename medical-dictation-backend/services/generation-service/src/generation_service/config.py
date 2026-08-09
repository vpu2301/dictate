"""generation-service configuration.

The inference backend is llama-server (llama.cpp) by default, NOT Ollama:
ADR-0036 records a constant ~420 ms/request scheduler overhead measured in
Ollama 0.32.5 with gemma3 (SWA cache forces full prompt re-processing), which
alone consumes the entire p95 <= 400 ms inline budget. Both backends sit
behind the same InferenceClient seam, so the choice is one env var.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env.local",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    service_name: str = "generation-service"
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

    # ── CORS (SPA integration) — explicit origins, mirror of auth-service A3.
    cors_allowed_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173,http://127.0.0.1:4173",
        alias="CORS_ALLOWED_ORIGINS",
    )

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    db_audit_writer_dsn: str = Field(
        default="postgresql://audit_writer:audit_writer@localhost:5432/medical_dictation",
        alias="DB_AUDIT_WRITER_DSN",
    )

    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")

    # ── Layer C feature gate ────────────────────────────────────────────
    # Global kill switch: when false the inline endpoint always answers 204
    # and no inference client is ever constructed (MDX_CONVERSATION_ENABLED
    # precedent — off means never touch the backend). The pod stays ready so
    # a disabled feature is not an outage.
    layer_c_enabled: bool = Field(default=True, alias="MDX_LAYER_C_ENABLED")
    # Comma-separated tenant UUIDs. Empty = every tenant (the kill switch
    # above is the off button); non-empty = only the listed tenants get
    # completions, everyone else gets a silent 204.
    tenant_allowlist: str = Field(default="", alias="MDX_GEN_TENANT_ALLOWLIST")

    @property
    def tenant_allowlist_set(self) -> frozenset[UUID]:
        return frozenset(
            UUID(t.strip()) for t in self.tenant_allowlist.split(",") if t.strip()
        )

    # ── Inference backend (ADR-0036) ────────────────────────────────────
    gen_backend: Literal["llamacpp", "ollama"] = Field(
        default="llamacpp", alias="MDX_GEN_BACKEND"
    )
    gen_base_url: str = Field(default="http://localhost:8089", alias="MDX_GEN_BASE_URL")
    # Model name is used by the Ollama backend (llama-server serves exactly
    # the model it was launched with) and echoed in responses/telemetry.
    gen_model: str = Field(default="gemma3:1b", alias="MDX_GEN_MODEL")
    gen_max_tokens: int = Field(default=24, alias="MDX_GEN_MAX_TOKENS")
    # Hard end-to-end budget: slot wait + inference. On expiry the endpoint
    # answers 204 — a late ghost completion is worthless to a typing
    # clinician. Production target p95 <= 400 ms is a rig-gated release gate
    # (ADR-0036); dev hosts may raise this to keep the feature usable.
    gen_timeout_ms: int = Field(default=600, alias="MDX_GEN_TIMEOUT_MS")
    # Dedicated inline concurrency slots so a future long-synthesis path on
    # the same model can never starve the typing path (and vice versa).
    gen_slots: int = Field(default=2, alias="MDX_GEN_SLOTS")

    # ── Rate limit (typing path) ────────────────────────────────────────
    # Sustained ~3 req/s with a burst of 10: dual fixed windows.
    rate_burst_per_second: int = Field(default=10, alias="MDX_GEN_RATE_BURST_PER_S")
    rate_per_10s: int = Field(default=30, alias="MDX_GEN_RATE_PER_10S")

    # Aggregated layer_c.completion.shown audit flush interval.
    shown_audit_flush_s: float = Field(default=600.0, alias="MDX_GEN_SHOWN_AUDIT_FLUSH_S")

    # ── Startup pre-warm (sprint 16 — sprint-03 retro cold-start) ───────
    # When on, the lifespan fires a 1-token completion at the inference
    # backend and /readyz stays 503 until it lands. Off in dev (llama.cpp
    # on a laptop warms in seconds; the dance isn't worth it there).
    prewarm_enabled: bool = Field(default=False, alias="MDX_PREWARM_ENABLED")
    prewarm_retry_seconds: float = Field(default=5.0, alias="MDX_PREWARM_RETRY_SECONDS")

    # ── Session revocation check (sprint 16) ────────────────────────────
    # When on, current_user rejects tokens whose sid/sub is on the Redis
    # denylist that auth-service pushes on logout/deactivation. Fail-OPEN
    # on Redis outage (ADR-0040). Same env name across the fleet; off in dev.
    session_revocation_enabled: bool = Field(
        default=False, alias="MDX_SESSION_REVOCATION_ENABLED"
    )


settings = Settings()
