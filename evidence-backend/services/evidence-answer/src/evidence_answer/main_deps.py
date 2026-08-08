"""Service state build/teardown (platform ServiceState pattern)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import asyncpg
import redis.asyncio as aioredis
from audit import AuditWriter
from crypto import Envelope, FileMasterKeyProvider, TenantKekRepository
from db import create_pool
from storage import EncryptedObjectStore, S3Client

from evidence_answer.adapters.retrieval import RetrievalClient
from evidence_answer.config import Settings, settings
from evidence_answer.domain.persist import AnswerPersister
from models import ModelGatewayClient

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Metrics:
    # spec §10: deflection rate by reason, answers by status.
    deflections: dict[str, int] = field(default_factory=dict)
    answers: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class ServiceState:
    settings: Settings
    app_pool: asyncpg.Pool
    audit_writer_pool: asyncpg.Pool
    crypto_pool: asyncpg.Pool
    audit_writer: AuditWriter
    redis: aioredis.Redis
    s3: S3Client
    object_store: EncryptedObjectStore
    gateway: ModelGatewayClient
    retrieval: RetrievalClient
    persister: AnswerPersister
    metrics: Metrics = field(default_factory=Metrics)


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
    object_store = EncryptedObjectStore(s3=s3, bucket=settings.s3_answers_bucket, envelope=envelope)

    redis_client = aioredis.from_url(settings.redis_url, decode_responses=False)
    gateway = ModelGatewayClient(
        settings.gateway_base_url, service_token=settings.gateway_service_token
    )
    retrieval = RetrievalClient(
        base_url=settings.retrieval_base_url,
        service_token=settings.retrieval_service_token,
        timeout_s=settings.web_timeout_s,
    )
    audit_writer = AuditWriter(audit_writer_pool)

    if not settings.web_enabled:
        logger.warning("EVA_ANSWER_WEB_ENABLED=false — answers are corpus-only")

    return ServiceState(
        settings=settings,
        app_pool=app_pool,
        audit_writer_pool=audit_writer_pool,
        crypto_pool=crypto_pool,
        audit_writer=audit_writer,
        redis=redis_client,
        s3=s3,
        object_store=object_store,
        gateway=gateway,
        retrieval=retrieval,
        persister=AnswerPersister(pool=app_pool, store=object_store, audit_writer=audit_writer),
    )


async def teardown_state(state: ServiceState) -> None:
    await state.retrieval.aclose()
    await state.gateway.aclose()
    await state.redis.aclose()
    await state.s3.aclose()
    await state.crypto_pool.close()
    await state.audit_writer_pool.close()
    await state.app_pool.close()
