"""HTTP client to ``nlp-service``.

Sprint 5 wires this on the final-emit path and on a lighter "partials"
path. The 200-ms per-call timeout is the hard ceiling: a slower NLP
response degrades to the raw Whisper output + a ``dictation.nlp_timeout``
audit row.

Why not unix-socket / shared-memory: in sprint 16 nlp-service moves
to its own pod for horizontal scaling; HTTP keeps the option open.
The 1-retry-on-503 policy avoids the failure-amplification class of
bug where a slow upstream gets hammered.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

import httpx

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class NlpClientConfig:
    base_url: str = "http://nlp-service:8000"
    timeout_seconds: float = 0.2
    partial_timeout_seconds: float = 0.05
    retry_on_503: bool = True


@dataclass(frozen=True, slots=True)
class NlpResult:
    """Subset of the nlp-service response that dictation-service consumes."""

    text: str
    words: list[dict[str, Any]]
    voice_commands: list[dict[str, Any]]
    operations: list[dict[str, Any]]
    confidence_spans: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    metadata: dict[str, Any]


class NlpClient:
    """Async HTTP client with a small connection pool."""

    def __init__(
        self,
        *,
        config: NlpClientConfig,
        service_token: str | None = None,
    ) -> None:
        self._config = config
        self._service_token = service_token
        headers = {"Authorization": f"Bearer {service_token}"} if service_token else {}
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=httpx.Timeout(
                connect=0.2, read=config.timeout_seconds, write=0.2, pool=0.2
            ),
            headers=headers,
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
        )

    async def process_final(
        self,
        *,
        tenant_id: UUID,
        text: str,
        words: list[dict[str, Any]],
        language: str,
        specialty: str | None,
        reference_date: date | None,
        template_sections: list[dict[str, Any]] | None = None,
    ) -> NlpResult | None:
        """Run the pipeline on a final segment. Returns None on timeout.

        Caller (dictation-service handler) treats None as a graceful
        degradation cue: emit the raw text + a ``dictation.nlp_timeout``
        audit row.
        """
        return await self._post(
            "/nlp/process",
            timeout=self._config.timeout_seconds,
            body={
                "text": text,
                "words": words,
                "language": language,
                "specialty": specialty,
                "reference_date": reference_date.isoformat() if reference_date else None,
                "is_partial": False,
                "template_sections": template_sections or [],
            },
        )

    async def process_partial(
        self,
        *,
        tenant_id: UUID,
        text: str,
        words: list[dict[str, Any]],
        language: str,
        specialty: str | None,
    ) -> NlpResult | None:
        """Lighter call: only stages 1 (voice commands) + 6 (confidence) run."""
        return await self._post(
            "/nlp/process",
            timeout=self._config.partial_timeout_seconds,
            body={
                "text": text,
                "words": words,
                "language": language,
                "specialty": specialty,
                "is_partial": True,
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── HTTP ───────────────────────────────────────────────────────

    async def _post(
        self, path: str, *, timeout: float, body: dict[str, Any]
    ) -> NlpResult | None:
        attempts = 2 if self._config.retry_on_503 else 1
        for attempt in range(attempts):
            try:
                resp = await asyncio.wait_for(
                    self._client.post(path, json=body), timeout=timeout
                )
            except (asyncio.TimeoutError, httpx.TimeoutException):
                logger.info(
                    "nlp.timeout",
                    extra={"path": path, "timeout_s": timeout, "attempt": attempt + 1},
                )
                return None
            except httpx.HTTPError as exc:
                logger.warning(
                    "nlp.transport_error",
                    extra={"path": path, "error_class": type(exc).__name__},
                )
                return None

            if resp.status_code == 200:
                doc = resp.json()
                return NlpResult(
                    text=doc.get("text", body["text"]),
                    words=list(doc.get("words", [])),
                    voice_commands=list(doc.get("voice_commands", [])),
                    operations=list(doc.get("operations", [])),
                    confidence_spans=list(doc.get("confidence_spans", [])),
                    warnings=list(doc.get("warnings", [])),
                    metadata=dict(doc.get("metadata", {})),
                )
            if resp.status_code == 503 and attempt + 1 < attempts:
                await asyncio.sleep(0.05)
                continue
            logger.warning(
                "nlp.non_200",
                extra={"path": path, "status": resp.status_code},
            )
            return None
        return None
