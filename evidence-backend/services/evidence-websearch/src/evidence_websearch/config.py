"""Config — the sole env surface of evidence-websearch (rule E8)."""

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

    service_name: str = "evidence-websearch"
    environment: str = Field(default="development", alias="ENVIRONMENT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    testing: bool = Field(default=False, alias="TESTING")
    otel_exporter_otlp_endpoint: str = Field(
        default="http://localhost:4317", alias="OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    otel_sdk_disabled: bool = Field(default=False, alias="OTEL_SDK_DISABLED")

    # Auth (inherited platform IdP, rule I1) — for the /web/domains admin surface.
    auth_issuer: str = Field(
        default="http://localhost:8088/realms/medical-dictation", alias="AUTH_ISSUER"
    )
    auth_jwks_url: str = Field(
        default="http://localhost:8088/realms/medical-dictation/protocol/openid-connect/certs",
        alias="AUTH_JWKS_URL",
    )
    auth_audience: str = Field(default="mdx-api", alias="AUTH_AUDIENCE")
    auth_clock_skew_seconds: int = Field(default=30, alias="AUTH_CLOCK_SKEW_SECONDS")

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
    db_pool_max_size: int = Field(default=10, alias="DB_POOL_MAX_SIZE")

    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")

    # Page snapshots go through libs/storage (rule E3). Web pages are not PHI,
    # but there is exactly one sanctioned blob path and reusing it costs
    # nothing — a cached third-party page ends up encrypted at rest too.
    s3_endpoint: str = Field(default="http://localhost:9000", alias="S3_ENDPOINT")
    s3_region: str = Field(default="us-east-1", alias="S3_REGION")
    s3_access_key: str = Field(default="minioadmin", alias="S3_ACCESS_KEY")
    s3_secret_key: str = Field(default="minioadmin", alias="S3_SECRET_KEY")
    s3_use_ssl: bool = Field(default=False, alias="S3_USE_SSL")
    s3_corpus_bucket: str = Field(default="eva-corpus", alias="S3_CORPUS_BUCKET")
    master_key_path: str = Field(default="/etc/mdx/master.key", alias="MDX_MASTER_KEY_PATH")

    gateway_base_url: str = Field(default="http://localhost:8015", alias="EVA_GATEWAY_BASE_URL")
    gateway_service_token: str | None = Field(default=None, alias="EVA_GATEWAY_SERVICE_TOKEN")

    # Internal service-to-service auth for POST /web/search (the caller is
    # evidence-answer, never a browser). None ⇒ dev-open + startup WARNING.
    service_token: str | None = Field(default=None, alias="EVA_WEBSEARCH_SERVICE_TOKEN")

    # ── metasearch ──────────────────────────────────────────────────────
    searxng_url: str = Field(default="http://localhost:8888", alias="EVA_SEARXNG_URL")
    searxng_timeout_s: float = Field(default=5.0, alias="EVA_SEARXNG_TIMEOUT_S")
    metasearch_results: int = Field(default=20, alias="EVA_METASEARCH_RESULTS")

    # ── egress proxy (THE only egress path, ADR-0005) ───────────────────
    # Every outbound request — metasearch included — leaves through here.
    # Empty means "no proxy configured": the fetcher then refuses to fetch
    # rather than opening a direct socket (fail closed, rule CS3 posture).
    egress_proxy_url: str = Field(default="", alias="EVA_EGRESS_PROXY_URL")
    # Escape hatch for the SSRF suite and for CI, where the fetcher is
    # pointed at a local fixture server and no proxy exists. Logs a startup
    # WARNING and is forbidden outside development (rule BE7).
    allow_direct_egress: bool = Field(default=False, alias="EVA_ALLOW_DIRECT_EGRESS")

    # ── fetch budgets (FR-7) ────────────────────────────────────────────
    fetch_timeout_s: float = Field(default=10.0, alias="EVA_FETCH_TIMEOUT_S")
    fetch_max_bytes: int = Field(default=3 * 1024 * 1024, alias="EVA_FETCH_MAX_BYTES")
    fetch_max_redirects: int = Field(default=3, alias="EVA_FETCH_MAX_REDIRECTS")
    fetch_concurrency: int = Field(default=4, alias="EVA_FETCH_CONCURRENCY")
    fetch_max_pages: int = Field(default=6, alias="EVA_FETCH_MAX_PAGES")
    robots_timeout_s: float = Field(default=3.0, alias="EVA_ROBOTS_TIMEOUT_S")
    user_agent: str = Field(
        default="evidentia-websearch/1.0 (+clinical evidence retrieval; respects robots.txt)",
        alias="EVA_FETCH_USER_AGENT",
    )

    # ── cache + ephemeral index ─────────────────────────────────────────
    page_cache_ttl_s: int = Field(default=24 * 3600, alias="EVA_WEB_PAGE_CACHE_TTL_S")
    ephemeral_ttl_s: int = Field(default=30 * 60, alias="EVA_EPHEMERAL_TTL_S")
    ephemeral_chunk_chars: int = Field(default=1200, alias="EVA_EPHEMERAL_CHUNK_CHARS")
    ephemeral_chunk_overlap: int = Field(default=150, alias="EVA_EPHEMERAL_CHUNK_OVERLAP")
    ephemeral_max_chunks_per_page: int = Field(default=40, alias="EVA_EPHEMERAL_MAX_CHUNKS")
    embed_batch_size: int = Field(default=32, alias="EVA_EMBED_BATCH_SIZE")

    # ── extraction quality ──────────────────────────────────────────────
    extract_min_chars: int = Field(default=400, alias="EVA_EXTRACT_MIN_CHARS")
    extract_max_link_density: float = Field(default=0.35, alias="EVA_EXTRACT_MAX_LINK_DENSITY")

    @property
    def is_development(self) -> bool:
        return self.environment == "development"


settings = Settings()
