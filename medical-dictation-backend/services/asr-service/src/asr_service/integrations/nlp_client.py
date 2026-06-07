"""HTTP client to ``nlp-service`` for batch enrichment.

The batch path is more tolerant of latency than streaming — a 1-minute
ASR job tolerates a 1-second NLP pass — so we use a longer timeout and
loop segments via the dedicated batch endpoint.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

import httpx

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class NlpBatchClientConfig:
    base_url: str = "http://nlp-service:8000"
    timeout_seconds: float = 10.0


class NlpBatchClient:
    def __init__(
        self,
        *,
        config: NlpBatchClientConfig,
        service_token: str | None = None,
    ) -> None:
        self._config = config
        headers = {"Authorization": f"Bearer {service_token}"} if service_token else {}
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=httpx.Timeout(
                connect=1.0,
                read=config.timeout_seconds,
                write=1.0,
                pool=1.0,
            ),
            headers=headers,
        )

    async def process_segments(
        self,
        *,
        tenant_id: UUID,
        segments: list[dict[str, Any]],
        language: str,
        specialty: str | None = None,
        reference_date: date | None = None,
    ) -> dict[str, Any] | None:
        try:
            resp = await self._client.post(
                "/nlp/process/batch",
                json={
                    "segments": segments,
                    "language": language,
                    "specialty": specialty,
                    "reference_date": (reference_date.isoformat() if reference_date else None),
                },
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "nlp_batch.transport_error",
                extra={"error_class": type(exc).__name__, "error": str(exc)},
            )
            return None
        if resp.status_code != 200:
            logger.warning(
                "nlp_batch.non_200",
                extra={"status": resp.status_code, "body": resp.text[:200]},
            )
            return None
        return resp.json()  # type: ignore[no-any-return]

    async def aclose(self) -> None:
        await self._client.aclose()
