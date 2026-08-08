"""Response cache (spec §1): Redis, deterministic key over everything the
determinism contract names. Degraded responses are never cached."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import redis.asyncio as aioredis

from evidence_models import RetrieveResponse


def cache_key(request_payload: dict[str, Any], *, lexicon_version: str, pins: str) -> str:
    canonical = json.dumps(
        {"req": request_payload, "lexicon": lexicon_version, "pins": pins},
        sort_keys=True,
        default=str,
    )
    return "eva:retrieve:" + hashlib.sha256(canonical.encode()).hexdigest()


class ResponseCache:
    def __init__(self, redis: aioredis.Redis, *, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    async def get(self, key: str) -> RetrieveResponse | None:
        try:
            raw = await self._redis.get(key)
        except Exception:  # noqa: BLE001 — cache is best-effort
            return None
        if raw is None:
            return None
        return RetrieveResponse.model_validate_json(raw)

    async def set(self, key: str, response: RetrieveResponse) -> None:
        if response.degraded:
            return
        try:
            await self._redis.set(key, response.model_dump_json(), ex=self._ttl)
        except Exception:  # noqa: BLE001
            return
