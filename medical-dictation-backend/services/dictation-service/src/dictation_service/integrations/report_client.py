"""HTTP client to ``report-service`` — draft creation on conversation
finalize (sprint 14).

The sprint-08 hand-off is explicit: conversation sessions create drafts
through the EXISTING ``POST /v1/reports`` — no parallel write path.
Draft creation is an *action* in report-service's domain, so it goes
over HTTP with the clinician's own bearer (the pattern set by
core-service → signing-service), never a shared service identity:
report-service enforces ``report.write`` on the actual author.

Failure policy mirrors the NLP client: never fail the finalize — the
transcript is already persisted; a missed draft is a
``conversation.draft.create_failed`` audit row and the clinician can
create the report from the session later.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ReportClientConfig:
    base_url: str = "http://report-service:8000"
    timeout_seconds: float = 5.0


@dataclass(frozen=True, slots=True)
class DraftResult:
    report_id: str
    code: str
    version_id: str


class ReportClient:
    def __init__(self, *, config: ReportClientConfig) -> None:
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=httpx.Timeout(
                connect=1.0,
                read=config.timeout_seconds,
                write=1.0,
                pool=1.0,
            ),
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=4),
        )

    async def create_draft(
        self,
        *,
        bearer: str,
        body: dict[str, Any],
    ) -> DraftResult | None:
        """POST /v1/reports with the clinician's bearer. None on any failure."""
        try:
            resp = await asyncio.wait_for(
                self._client.post(
                    "/v1/reports",
                    json=body,
                    headers={"Authorization": f"Bearer {bearer}"},
                ),
                timeout=self._config.timeout_seconds + 1.0,
            )
        except (TimeoutError, httpx.HTTPError) as exc:
            logger.warning(
                "report.draft_transport_error", extra={"error_class": type(exc).__name__}
            )
            return None
        if resp.status_code != 201:
            logger.warning(
                "report.draft_non_201",
                extra={"status": resp.status_code, "body": resp.text[:512]},
            )
            return None
        doc = resp.json()
        return DraftResult(
            report_id=str(doc.get("id", "")),
            code=str(doc.get("code", "")),
            version_id=str(doc.get("version_id", "")),
        )

    async def aclose(self) -> None:
        await self._client.aclose()
