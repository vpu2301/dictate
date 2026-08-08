"""Service state build/teardown (platform ServiceState pattern)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg
import redis.asyncio as aioredis
from audit import AuditWriter
from crypto import Envelope, FileMasterKeyProvider, TenantKekRepository
from db import create_pool
from messaging import RedisStreamsConsumer, RedisStreamsProducer
from storage import EncryptedObjectStore, S3Client

from evidence_ingest.adapters.clamav import ClamAvScanner
from evidence_ingest.adapters.search import LexicalIndex
from evidence_ingest.adapters.store import CorpusStore
from evidence_ingest.config import Settings, settings
from evidence_ingest.domain.pipeline import PipelineContext
from models import ModelGatewayClient

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ServiceState:
    settings: Settings
    app_pool: asyncpg.Pool
    audit_writer_pool: asyncpg.Pool
    crypto_pool: asyncpg.Pool
    audit_writer: AuditWriter
    s3: S3Client
    object_store: EncryptedObjectStore
    corpus_store: CorpusStore
    redis: aioredis.Redis
    producer: RedisStreamsProducer
    lexical: LexicalIndex
    gateway: ModelGatewayClient
    scanner: ClamAvScanner | None
    pipeline: PipelineContext


_state: ServiceState | None = None


def install_state(state: ServiceState) -> None:
    global _state
    _state = state


def get_state() -> ServiceState:
    if _state is None:
        raise RuntimeError("service state not initialised")
    return _state


async def build_state() -> ServiceState:
    app_pool = await create_pool(
        settings.db_app_role_dsn,
        application_name=f"{settings.service_name}/app",
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
    )
    audit_writer_pool = await create_pool(
        settings.db_audit_writer_dsn,
        application_name=f"{settings.service_name}/audit_writer",
        min_size=1,
        max_size=4,
    )
    crypto_pool = await create_pool(
        settings.db_crypto_writer_dsn,
        application_name=f"{settings.service_name}/crypto_writer",
        min_size=1,
        max_size=4,
    )

    master = FileMasterKeyProvider(path=settings.master_key_path)
    await master.startup_self_check()
    kek_repo = TenantKekRepository(pool=crypto_pool, master_key_provider=master)
    envelope = Envelope(master_key_provider=master, kek_repository=kek_repo)
    s3 = S3Client(
        endpoint_url=settings.s3_endpoint,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        region=settings.s3_region,
        use_ssl=settings.s3_use_ssl,
    )
    object_store = EncryptedObjectStore(s3=s3, bucket=settings.s3_corpus_bucket, envelope=envelope)
    corpus_store = CorpusStore(object_store)

    redis_client = aioredis.from_url(settings.redis_url, decode_responses=False)
    producer = RedisStreamsProducer(client=redis_client, default_stream=settings.ingest_stream)

    lexical = LexicalIndex(url=settings.opensearch_url, index=settings.opensearch_index)
    gateway = ModelGatewayClient(
        settings.gateway_base_url, service_token=settings.gateway_service_token
    )

    scanner: ClamAvScanner | None = None
    if settings.virus_scan_enabled:
        scanner = ClamAvScanner(host=settings.clamd_host, port=settings.clamd_port)
    else:
        logger.warning(
            "EVA_VIRUS_SCAN_ENABLED=false — uploaded corpus files are NOT virus-scanned. "
            "Local dev only; forbidden outside development (rule BE7)."
        )

    audit_writer = AuditWriter(audit_writer_pool)

    pipeline = PipelineContext(
        settings=settings,
        app_pool=app_pool,
        corpus_store=corpus_store,
        object_store=object_store,
        lexical=lexical,
        gateway=gateway,
        audit_writer=audit_writer,
        scanner=scanner,
    )

    return ServiceState(
        settings=settings,
        app_pool=app_pool,
        audit_writer_pool=audit_writer_pool,
        crypto_pool=crypto_pool,
        audit_writer=audit_writer,
        s3=s3,
        object_store=object_store,
        corpus_store=corpus_store,
        redis=redis_client,
        producer=producer,
        lexical=lexical,
        gateway=gateway,
        scanner=scanner,
        pipeline=pipeline,
    )


def build_consumer(state: ServiceState) -> RedisStreamsConsumer:
    return RedisStreamsConsumer(
        client=state.redis,
        producer=state.producer,
        stream=state.settings.ingest_stream,
        group=state.settings.ingest_group,
        consumer=state.settings.ingest_consumer_name,
        dlq_stream=state.settings.ingest_dlq_stream,
        max_retries=state.settings.ingest_max_retries,
    )


async def teardown_state(state: ServiceState) -> None:
    await state.gateway.aclose()
    await state.lexical.aclose()
    await state.redis.aclose()
    await state.s3.aclose()
    await state.crypto_pool.close()
    await state.audit_writer_pool.close()
    await state.app_pool.close()
