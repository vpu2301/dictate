"""Service-wide singletons for asr-service.

Constructed in main.py's lifespan; consumed by routers via deps.get_state.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg
import redis.asyncio as aioredis

from audit import AuditWriter
from auth import JwksCache
from crypto import Envelope, TenantKekRepository, build_master_key_provider
from db import create_pool
from messaging import RedisStreamsProducer
from storage import EncryptedObjectStore, S3Client

from .config import settings
from .integrations.nlp_client import NlpBatchClient, NlpBatchClientConfig


@dataclass
class ServiceState:
    """Container for runtime singletons. Stored on ``app.state.svc``."""

    jwks_cache: JwksCache
    app_pool: asyncpg.Pool
    audit_writer_pool: asyncpg.Pool
    crypto_pool: asyncpg.Pool
    audit_writer: AuditWriter
    redis: aioredis.Redis
    queue_producer: RedisStreamsProducer
    s3: S3Client
    audio_store: EncryptedObjectStore
    transcript_store: EncryptedObjectStore
    envelope: Envelope
    nlp_client: NlpBatchClient


async def build_state() -> ServiceState:
    """Construct every async resource the service needs."""
    jwks_cache = JwksCache(issuer_to_url={settings.auth_issuer: settings.auth_jwks_url})

    app_pool = await create_pool(
        settings.db_app_role_dsn,
        application_name=f"{settings.service_name}/app",
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
    )
    audit_writer_pool = await create_pool(
        settings.db_audit_writer_dsn,
        application_name=f"{settings.service_name}/audit_writer",
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
    )
    crypto_pool = await create_pool(
        settings.db_crypto_writer_dsn,
        application_name=f"{settings.service_name}/crypto_writer",
        min_size=1,
        max_size=4,
    )

    master = build_master_key_provider(
        provider=settings.master_key_provider,
        file_path=settings.master_key_path,
        vault_addr=settings.vault_addr,
        vault_token=settings.vault_token,
        vault_transit_key=settings.vault_transit_key,
        vault_transit_mount=settings.vault_transit_mount,
    )
    await master.startup_self_check()

    kek_repo = TenantKekRepository(pool=crypto_pool, master_key_provider=master)
    envelope = Envelope(master_key_provider=master, kek_repository=kek_repo)

    redis_client = aioredis.from_url(settings.redis_url, decode_responses=False)
    producer = RedisStreamsProducer(
        client=redis_client,
        default_stream=settings.asr_jobs_stream,
        maxlen=settings.asr_jobs_maxlen,
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

    nlp_client = NlpBatchClient(
        config=NlpBatchClientConfig(
            base_url=settings.nlp_base_url,
            timeout_seconds=settings.nlp_timeout_seconds,
        )
    )

    return ServiceState(
        jwks_cache=jwks_cache,
        app_pool=app_pool,
        audit_writer_pool=audit_writer_pool,
        crypto_pool=crypto_pool,
        audit_writer=AuditWriter(audit_writer_pool),
        redis=redis_client,
        queue_producer=producer,
        s3=s3,
        audio_store=audio_store,
        transcript_store=transcript_store,
        envelope=envelope,
        nlp_client=nlp_client,
    )


async def teardown_state(state: ServiceState) -> None:
    await state.nlp_client.aclose()
    await state.jwks_cache.aclose()
    await state.queue_producer.aclose()
    await state.redis.aclose()
    await state.app_pool.close()
    await state.audit_writer_pool.close()
    await state.crypto_pool.close()
    await state.s3.aclose()
