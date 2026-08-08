"""Config — the sole env surface of evidence-retrieval (rule E8)."""

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

    service_name: str = "evidence-retrieval"
    environment: str = Field(default="development", alias="ENVIRONMENT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    testing: bool = Field(default=False, alias="TESTING")
    otel_exporter_otlp_endpoint: str = Field(
        default="http://localhost:4317", alias="OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    otel_sdk_disabled: bool = Field(default=False, alias="OTEL_SDK_DISABLED")

    db_app_role_dsn: str = Field(
        default="postgresql://app_role:app_role@localhost:5432/medical_dictation",
        alias="DB_APP_ROLE_DSN",
    )
    db_pool_min_size: int = Field(default=1, alias="DB_POOL_MIN_SIZE")
    db_pool_max_size: int = Field(default=10, alias="DB_POOL_MAX_SIZE")

    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")

    opensearch_url: str = Field(default="http://localhost:9200", alias="EVA_OPENSEARCH_URL")
    opensearch_index: str = Field(default="evidence-chunks", alias="EVA_OPENSEARCH_INDEX")

    gateway_base_url: str = Field(default="http://localhost:8015", alias="EVA_GATEWAY_BASE_URL")
    gateway_service_token: str | None = Field(default=None, alias="EVA_GATEWAY_SERVICE_TOKEN")

    # Internal service-to-service auth (rule §7: identity attaches in
    # evidence-answer, not here). None ⇒ dev-open with a startup WARNING.
    service_token: str | None = Field(default=None, alias="EVA_RETRIEVAL_SERVICE_TOKEN")

    # ── CORS ────────────────────────────────────────────────────────────
    # DELIBERATELY EMPTY BY DEFAULT, unlike the platform services (which ship
    # the SPA's dev origins in their own defaults). /retrieve is an internal
    # hop: the browser-facing caller is evidence-answer, and the only browser
    # that has business here is a developer's, driving the dictat retrieval
    # playground (#/evidence/dev/retrieval) against a dev-open instance.
    #
    # So this is opt-in per environment rather than on-by-default, and a
    # non-empty value in anything but development is a misconfiguration the
    # startup path shouts about. Explicit origins only — never "*" — because
    # allow_credentials=True forbids the wildcard (mirror of report-service).
    cors_allowed_origins: str = Field(default="", alias="CORS_ALLOWED_ORIGINS")

    # Budgets (spec §4/§5; the 400 ms rerank budget is the rig target — dev
    # CPU overrides via env, degrade path covers the rest).
    k_max: int = Field(default=50, alias="EVA_RETRIEVAL_K_MAX")
    local_connector_timeout_ms: int = Field(default=600, alias="EVA_RETRIEVAL_LOCAL_TIMEOUT_MS")
    rerank_budget_ms: int = Field(default=400, alias="EVA_RETRIEVAL_RERANK_BUDGET_MS")
    rerank_batch_size: int = Field(default=32, alias="EVA_RETRIEVAL_RERANK_BATCH")
    rerank_candidates: int = Field(default=32, alias="EVA_RETRIEVAL_RERANK_CANDIDATES")
    candidate_pool_per_engine: int = Field(default=50, alias="EVA_RETRIEVAL_POOL_PER_ENGINE")

    # ── web connector (S04) ─────────────────────────────────────────────
    # Talks to evidence-websearch, which owns the egress proxy. The budget is
    # far above the local connectors' because a cold web fetch is a network
    # round trip to a third party; the answer pipeline runs it off the
    # critical path (late sources) rather than making the user wait.
    websearch_base_url: str = Field(default="http://localhost:8014", alias="EVA_WEBSEARCH_BASE_URL")
    websearch_service_token: str | None = Field(default=None, alias="EVA_WEBSEARCH_SERVICE_TOKEN")
    web_connector_timeout_ms: int = Field(default=25_000, alias="EVA_RETRIEVAL_WEB_TIMEOUT_MS")

    cache_ttl_seconds: int = Field(default=120, alias="EVA_RETRIEVAL_CACHE_TTL_S")
    lexicon_version: str = "1.0"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def is_development(self) -> bool:
        return self.environment == "development"


settings = Settings()
