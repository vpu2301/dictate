"""Service state build/teardown (platform ServiceState pattern)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import asyncpg
import httpx
import redis.asyncio as aioredis
from audit import AuditWriter
from crypto import Envelope, FileMasterKeyProvider, TenantKekRepository
from db import create_pool
from storage import EncryptedObjectStore, S3Client

from evidence_websearch.config import Settings, settings
from evidence_websearch.domain.cache import PageCache
from evidence_websearch.domain.connector import WebSearchRuntime
from evidence_websearch.domain.ephemeral import EphemeralIndex
from evidence_websearch.domain.fetcher import PageFetcher
from evidence_websearch.domain.metasearch import MetaSearchClient
from models import ModelGatewayClient

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Metrics:
    # web fetch outcome counters (spec §10): ok / robots_skip / paywalled /
    # garbage / quarantined / error, keyed by web_pages.status.
    fetch_outcomes: dict[str, int] = field(default_factory=dict)
    index_bytes: int = 0


@dataclass(slots=True)
class ServiceState:
    settings: Settings
    app_pool: asyncpg.Pool
    audit_writer_pool: asyncpg.Pool
    crypto_pool: asyncpg.Pool
    audit_writer: AuditWriter
    redis: aioredis.Redis
    s3: S3Client
    gateway: ModelGatewayClient
    egress: httpx.AsyncClient
    metasearch: MetaSearchClient
    runtime: WebSearchRuntime
    metrics: Metrics = field(default_factory=Metrics)


_state: ServiceState | None = None


def install_state(state: ServiceState) -> None:
    global _state
    _state = state


def get_state() -> ServiceState:
    if _state is None:
        raise RuntimeError("service state not initialised")
    return _state


def build_egress_client(config: Settings) -> httpx.AsyncClient:
    """The one HTTP client allowed to leave the cluster.

    With no proxy configured and no explicit dev override, the client is
    pointed at an unroutable address so a coding mistake fails loudly instead
    of quietly opening a direct socket to the internet (ADR-0005: the proxy is
    the ONLY egress).
    """
    if config.egress_proxy_url:
        return httpx.AsyncClient(
            proxy=config.egress_proxy_url,
            timeout=config.fetch_timeout_s,
            follow_redirects=False,
            trust_env=False,
        )
    if config.allow_direct_egress:
        logger.warning(
            "EVA_ALLOW_DIRECT_EGRESS=true — web fetches bypass the egress proxy. "
            "Local dev and the SSRF fixture suite only; forbidden outside "
            "development (rule BE7)."
        )
        return httpx.AsyncClient(
            timeout=config.fetch_timeout_s, follow_redirects=False, trust_env=False
        )
    logger.error(
        "EVA_EGRESS_PROXY_URL is unset — the web connector is disabled. "
        "Answers degrade to corpus-only."
    )
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(retries=0),
        base_url="http://127.0.0.1:9",  # discard port: every fetch fails fast
        timeout=1.0,
        follow_redirects=False,
        trust_env=False,
    )


async def build_state() -> ServiceState:
    if settings.service_token is None:
        logger.warning(
            "EVA_WEBSEARCH_SERVICE_TOKEN unset — /web/search is unauthenticated. "
            "Local dev only; forbidden outside development (rule BE7)."
        )
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

    redis_client = aioredis.from_url(settings.redis_url, decode_responses=False)
    gateway = ModelGatewayClient(
        settings.gateway_base_url, service_token=settings.gateway_service_token
    )
    egress = build_egress_client(settings)
    metasearch = MetaSearchClient(
        base_url=settings.searxng_url,
        timeout_s=settings.searxng_timeout_s,
        results=settings.metasearch_results,
    )
    fetcher = PageFetcher(
        client=egress,
        allow_http=settings.allow_direct_egress,
        timeout_s=settings.fetch_timeout_s,
        max_bytes=settings.fetch_max_bytes,
        max_redirects=settings.fetch_max_redirects,
        robots_timeout_s=settings.robots_timeout_s,
        user_agent=settings.user_agent,
    )
    runtime = WebSearchRuntime(
        settings=settings,
        pool=app_pool,
        metasearch=metasearch,
        fetcher=fetcher,
        cache=PageCache(object_store),
        index=EphemeralIndex(
            redis=redis_client,
            gateway=gateway,
            ttl_seconds=settings.ephemeral_ttl_s,
            batch_size=settings.embed_batch_size,
        ),
    )
    return ServiceState(
        settings=settings,
        app_pool=app_pool,
        audit_writer_pool=audit_writer_pool,
        crypto_pool=crypto_pool,
        audit_writer=AuditWriter(audit_writer_pool),
        redis=redis_client,
        s3=s3,
        gateway=gateway,
        egress=egress,
        metasearch=metasearch,
        runtime=runtime,
    )


async def teardown_state(state: ServiceState) -> None:
    await state.metasearch.aclose()
    await state.egress.aclose()
    await state.gateway.aclose()
    await state.redis.aclose()
    await state.s3.aclose()
    await state.crypto_pool.close()
    await state.audit_writer_pool.close()
    await state.app_pool.close()
