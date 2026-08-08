"""Service state build/teardown."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import asyncpg
import httpx
import redis.asyncio as aioredis
from db import create_pool

from evidence_retrieval.adapters.opensearch import LexicalSearch
from evidence_retrieval.config import Settings, settings
from evidence_retrieval.domain.cache import ResponseCache
from evidence_retrieval.domain.connectors.stubs import UnavailableSource, all_stubs
from models import ModelGatewayClient

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Metrics:
    rerank_degraded: int = 0


@dataclass(slots=True)
class ServiceState:
    settings: Settings
    app_pool: asyncpg.Pool
    redis: aioredis.Redis
    lexical: LexicalSearch
    gateway: ModelGatewayClient
    cache: ResponseCache
    # HTTP hop to evidence-websearch (the `web` connector, S04).
    websearch: httpx.AsyncClient
    stubs: list[UnavailableSource]
    # Model-pin fingerprint for the determinism/cache contract.
    pins: str
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
    if settings.service_token is None:
        logger.warning(
            "EVA_RETRIEVAL_SERVICE_TOKEN unset — /retrieve is unauthenticated. "
            "Local dev only; forbidden outside development (rule BE7)."
        )
    app_pool = await create_pool(
        settings.db_app_role_dsn,
        application_name=f"{settings.service_name}/app",
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
    )
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=False)
    lexical = LexicalSearch(url=settings.opensearch_url, index=settings.opensearch_index)
    gateway = ModelGatewayClient(
        settings.gateway_base_url, service_token=settings.gateway_service_token
    )
    websearch = httpx.AsyncClient(
        base_url=settings.websearch_base_url,
        timeout=settings.web_connector_timeout_ms / 1000,
    )
    pins = "embed=BAAI/bge-m3;rerank=BAAI/bge-reranker-v2-m3"  # docs/models/PINS.md
    return ServiceState(
        settings=settings,
        app_pool=app_pool,
        redis=redis_client,
        lexical=lexical,
        gateway=gateway,
        cache=ResponseCache(redis_client, ttl_seconds=settings.cache_ttl_seconds),
        websearch=websearch,
        stubs=all_stubs(),
        pins=pins,
    )


async def teardown_state(state: ServiceState) -> None:
    await state.websearch.aclose()
    await state.gateway.aclose()
    await state.lexical.aclose()
    await state.redis.aclose()
    await state.app_pool.close()
