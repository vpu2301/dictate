"""Config — the sole env surface of evidence-ingest (rule E8)."""

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

    service_name: str = "evidence-ingest"
    environment: str = Field(default="development", alias="ENVIRONMENT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    testing: bool = Field(default=False, alias="TESTING")
    otel_exporter_otlp_endpoint: str = Field(
        default="http://localhost:4317", alias="OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    otel_sdk_disabled: bool = Field(default=False, alias="OTEL_SDK_DISABLED")

    # Auth (inherited platform IdP, rule I1)
    auth_issuer: str = Field(
        default="http://localhost:8088/realms/medical-dictation", alias="AUTH_ISSUER"
    )
    auth_jwks_url: str = Field(
        default="http://localhost:8088/realms/medical-dictation/protocol/openid-connect/certs",
        alias="AUTH_JWKS_URL",
    )
    auth_audience: str = Field(default="mdx-api", alias="AUTH_AUDIENCE")
    auth_clock_skew_seconds: int = Field(default=30, alias="AUTH_CLOCK_SKEW_SECONDS")

    # Database (platform DSN conventions)
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

    # Object store (corpus artifacts through EncryptedObjectStore, rule E3)
    s3_endpoint: str = Field(default="http://localhost:9000", alias="S3_ENDPOINT")
    s3_region: str = Field(default="us-east-1", alias="S3_REGION")
    s3_access_key: str = Field(default="minioadmin", alias="S3_ACCESS_KEY")
    s3_secret_key: str = Field(default="minioadmin", alias="S3_SECRET_KEY")
    s3_use_ssl: bool = Field(default=False, alias="S3_USE_SSL")
    s3_corpus_bucket: str = Field(default="eva-corpus", alias="S3_CORPUS_BUCKET")
    master_key_path: str = Field(default="/etc/mdx/master.key", alias="MDX_MASTER_KEY_PATH")

    # Queue
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    ingest_stream: str = Field(default="evidence:ingest", alias="EVA_INGEST_STREAM")
    ingest_group: str = Field(default="ingest-workers", alias="EVA_INGEST_GROUP")
    ingest_dlq_stream: str = Field(default="evidence:ingest:dlq", alias="EVA_INGEST_DLQ_STREAM")
    ingest_consumer_name: str = Field(default="ingest-1", alias="EVA_INGEST_CONSUMER_NAME")
    ingest_max_retries: int = Field(default=3, alias="EVA_INGEST_MAX_RETRIES")

    # Model gateway (rule E12 — the only model path)
    gateway_base_url: str = Field(default="http://localhost:8015", alias="EVA_GATEWAY_BASE_URL")
    gateway_service_token: str | None = Field(default=None, alias="EVA_GATEWAY_SERVICE_TOKEN")
    embed_batch_size: int = Field(default=64, alias="EVA_EMBED_BATCH_SIZE")

    # Lexical index
    opensearch_url: str = Field(default="http://localhost:9200", alias="EVA_OPENSEARCH_URL")
    opensearch_index: str = Field(default="evidence-chunks", alias="EVA_OPENSEARCH_INDEX")

    # Parsing limits (sandbox budgets around pure parsers)
    max_upload_bytes: int = Field(default=50 * 1024 * 1024, alias="EVA_INGEST_MAX_UPLOAD_BYTES")
    parse_timeout_seconds: float = Field(default=120.0, alias="EVA_INGEST_PARSE_TIMEOUT_S")

    # Chunking targets (structure-aware, tokens approximated by words)
    chunk_target_tokens: int = Field(default=500, alias="EVA_CHUNK_TARGET_TOKENS")
    chunk_max_tokens: int = Field(default=700, alias="EVA_CHUNK_MAX_TOKENS")
    chunk_min_tokens: int = Field(default=350, alias="EVA_CHUNK_MIN_TOKENS")

    # Virus scan (clamd INSTREAM). Dev-off logs a startup WARNING (rule BE7).
    virus_scan_enabled: bool = Field(default=False, alias="EVA_VIRUS_SCAN_ENABLED")
    clamd_host: str = Field(default="localhost", alias="EVA_CLAMD_HOST")
    clamd_port: int = Field(default=3310, alias="EVA_CLAMD_PORT")

    @property
    def is_development(self) -> bool:
        return self.environment == "development"


settings = Settings()
