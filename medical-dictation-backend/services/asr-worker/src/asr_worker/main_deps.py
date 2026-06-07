"""Service-wide singletons for asr-worker."""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg
import redis.asyncio as aioredis

from audit import AuditWriter
from crypto import Envelope, FileMasterKeyProvider, TenantKekRepository
from db import create_pool
from messaging import RedisStreamsConsumer, RedisStreamsProducer
from storage import EncryptedObjectStore, S3Client

from .config import settings
from .inference import WhisperEngine


@dataclass
class WorkerState:
    app_pool: asyncpg.Pool
    audit_writer_pool: asyncpg.Pool
    crypto_pool: asyncpg.Pool
    audit_writer: AuditWriter
    redis: aioredis.Redis
    producer: RedisStreamsProducer
    consumer: RedisStreamsConsumer
    s3: S3Client
    audio_store: EncryptedObjectStore
    transcript_store: EncryptedObjectStore
    envelope: Envelope
    engine: WhisperEngine


async def build_state() -> WorkerState:
    app_pool = await create_pool(
        settings.db_app_role_dsn,
        application_name=f"{settings.service_name}/app",
        min_size=1,
        max_size=4,
    )
    audit_writer_pool = await create_pool(
        settings.db_audit_writer_dsn,
        application_name=f"{settings.service_name}/audit_writer",
        min_size=1,
        max_size=2,
    )
    crypto_pool = await create_pool(
        settings.db_crypto_writer_dsn,
        application_name=f"{settings.service_name}/crypto_writer",
        min_size=1,
        max_size=2,
    )
    master = FileMasterKeyProvider(path=settings.master_key_path)
    await master.startup_self_check()

    kek_repo = TenantKekRepository(pool=crypto_pool, master_key_provider=master)
    envelope = Envelope(master_key_provider=master, kek_repository=kek_repo)

    redis_client = aioredis.from_url(settings.redis_url, decode_responses=False)
    producer = RedisStreamsProducer(client=redis_client, default_stream=settings.asr_jobs_stream)
    consumer = RedisStreamsConsumer(
        client=redis_client,
        producer=producer,
        stream=settings.asr_jobs_stream,
        group=settings.asr_jobs_group,
        consumer=settings.worker_consumer_name,
        dlq_stream=settings.asr_jobs_dlq_stream,
        reclaim_idle_ms=settings.asr_jobs_idle_reclaim_ms,
        max_retries=settings.asr_jobs_max_retries,
    )

    s3 = S3Client(
        endpoint_url=settings.s3_endpoint,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        region=settings.s3_region,
        use_ssl=settings.s3_use_ssl,
    )
    audio_store = EncryptedObjectStore(s3=s3, bucket=settings.s3_audio_bucket, envelope=envelope)
    transcript_store = EncryptedObjectStore(
        s3=s3, bucket=settings.s3_transcripts_bucket, envelope=envelope
    )

    engine = WhisperEngine()
    engine.load()

    return WorkerState(
        app_pool=app_pool,
        audit_writer_pool=audit_writer_pool,
        crypto_pool=crypto_pool,
        audit_writer=AuditWriter(audit_writer_pool),
        redis=redis_client,
        producer=producer,
        consumer=consumer,
        s3=s3,
        audio_store=audio_store,
        transcript_store=transcript_store,
        envelope=envelope,
        engine=engine,
    )


async def teardown_state(state: WorkerState) -> None:
    await state.producer.aclose()
    await state.redis.aclose()
    await state.app_pool.close()
    await state.audit_writer_pool.close()
    await state.crypto_pool.close()
    await state.s3.aclose()
