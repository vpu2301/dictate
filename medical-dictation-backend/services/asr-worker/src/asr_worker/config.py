"""asr-worker configuration."""

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

    service_name: str = "asr-worker"
    environment: str = Field(default="development", alias="ENVIRONMENT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    testing: bool = Field(default=False, alias="TESTING")

    # OpenTelemetry
    otel_exporter_otlp_endpoint: str = Field(
        default="http://localhost:4317", alias="OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    otel_sdk_disabled: bool = Field(default=False, alias="OTEL_SDK_DISABLED")

    # ── Whisper / device ────────────────────────────────────────────────
    asr_device: str = Field(default="cuda", alias="MD_ASR_DEVICE")  # cuda | cpu
    asr_model: str = Field(default="large-v3", alias="MD_ASR_MODEL")
    asr_compute_type: str = Field(default="float16", alias="MD_ASR_COMPUTE_TYPE")
    asr_beam_size: int = Field(default=5, alias="MD_ASR_BEAM_SIZE")

    # ── Model sourcing / pinning (Sprint B1 Day 1, ADR-0021) ────────────
    # These are build-time provenance knobs. Defaults are a no-op for the
    # runtime: `asr_model` above still selects the weights (a HF id like
    # "large-v3" in dev, or the baked local dir "/opt/models/whisper-large-v3"
    # in the pinned GPU image). The fields below make a running worker
    # self-describing — it can log exactly which repo@revision it was built
    # from — and let `inference.py` reject an unsupported engine early.
    asr_engine: str = Field(default="faster_whisper", alias="MD_ASR_ENGINE")
    asr_model_repo: str = Field(
        default="Systran/faster-whisper-large-v3", alias="MD_ASR_MODEL_REPO"
    )
    asr_model_revision: str = Field(default="", alias="MD_ASR_MODEL_REVISION")
    asr_model_sha256: str = Field(default="", alias="MD_ASR_MODEL_SHA256")
    asr_max_inference_seconds_multiplier: float = Field(
        default=5.0, alias="MD_ASR_MAX_INFERENCE_SECONDS_MULTIPLIER"
    )
    asr_jobs_before_recycle: int = Field(default=100, alias="MD_ASR_JOBS_BEFORE_RECYCLE")

    # ── Database / queue / storage ──────────────────────────────────────
    db_app_role_dsn: str = Field(
        default="postgresql://app_role:app_role@postgres:5432/medical_dictation",
        alias="DB_APP_ROLE_DSN",
    )
    db_audit_writer_dsn: str = Field(
        default="postgresql://audit_writer:audit_writer@postgres:5432/medical_dictation",
        alias="DB_AUDIT_WRITER_DSN",
    )
    db_crypto_writer_dsn: str = Field(
        default="postgresql://crypto_writer:crypto_writer@postgres:5432/medical_dictation",
        alias="DB_CRYPTO_WRITER_DSN",
    )

    redis_url: str = Field(default="redis://redis:6379/0", alias="REDIS_URL")
    asr_jobs_stream: str = Field(default="asr:jobs", alias="MD_ASR_JOBS_STREAM")
    asr_jobs_dlq_stream: str = Field(default="asr:jobs:dlq", alias="MD_ASR_JOBS_DLQ_STREAM")
    asr_jobs_group: str = Field(default="asr-workers", alias="MD_ASR_JOBS_GROUP")
    asr_jobs_max_retries: int = Field(default=3, alias="MD_ASR_JOBS_MAX_RETRIES")
    asr_jobs_idle_reclaim_ms: int = Field(default=60_000, alias="MD_ASR_JOBS_IDLE_RECLAIM_MS")

    s3_endpoint: str = Field(default="http://minio:9000", alias="S3_ENDPOINT")
    s3_region: str = Field(default="us-east-1", alias="S3_REGION")
    s3_access_key: str = Field(default="minioadmin", alias="S3_ACCESS_KEY")
    s3_secret_key: str = Field(default="minioadmin", alias="S3_SECRET_KEY")
    s3_audio_bucket: str = Field(default="mdx-audio", alias="S3_AUDIO_BUCKET")
    s3_transcripts_bucket: str = Field(default="mdx-transcripts", alias="S3_TRANSCRIPTS_BUCKET")
    s3_use_ssl: bool = Field(default=False, alias="S3_USE_SSL")

    master_key_path: str = Field(default="/etc/mdx/master.key", alias="MDX_MASTER_KEY_PATH")

    ffmpeg_path: str = Field(default="ffmpeg", alias="MD_ASR_FFMPEG_PATH")
    ffmpeg_timeout_seconds: float = Field(default=30.0, alias="MD_ASR_FFMPEG_TIMEOUT_SECONDS")

    worker_consumer_name: str = Field(default="worker-1", alias="MD_ASR_WORKER_NAME")


settings = Settings()
