"""HTTP client to report-service for template lookup.

Sprint-06 lookup happens once at session start (full template loaded
into session context) and once per section switch (cached on session).
The dictation hot path doesn't re-fetch; report-service's own
TTLCache absorbs cross-session repeats.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TemplateClientConfig:
    base_url: str = "http://report-service:8000"
    timeout_seconds: float = 1.0


@dataclass(frozen=True, slots=True)
class TemplateDoc:
    template_id: UUID
    code: str
    name: str
    language: str
    specialty: str
    schema_version: int
    sections: list[dict[str, Any]]


class TemplateClient:
    def __init__(
        self,
        *,
        config: TemplateClientConfig,
        bearer_token_provider: object | None = None,
    ) -> None:
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=httpx.Timeout(
                connect=0.3,
                read=config.timeout_seconds,
                write=0.3,
                pool=0.3,
            ),
        )
        self._token_provider = bearer_token_provider

    async def fetch(self, *, template_id: UUID, bearer: str) -> TemplateDoc | None:
        try:
            resp = await self._client.get(
                f"/templates/{template_id}",
                headers={"Authorization": f"Bearer {bearer}"},
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "template_client.transport_error",
                extra={"error_class": type(exc).__name__, "error": str(exc)},
            )
            return None
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            logger.warning(
                "template_client.non_200",
                extra={"status": resp.status_code, "body": resp.text[:200]},
            )
            return None
        doc = resp.json()
        schema = doc["schema_jsonb"]
        return TemplateDoc(
            template_id=UUID(doc["id"]),
            code=schema.get("code", doc["code"]),
            name=schema.get("name", doc["name"]),
            language=schema.get("language", doc["language"]),
            specialty=schema.get("specialty", doc["specialty"]),
            schema_version=int(doc.get("schema_version", 1)),
            sections=list(schema.get("sections", [])),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def section_prompt(doc: TemplateDoc, section_id: str) -> tuple[str, str] | None:
    """Return (prompt, section_name) for ``section_id``; None if absent."""
    for s in doc.sections:
        if s.get("id") == section_id:
            return s.get("asr_prompt", ""), s.get("name", section_id)
    return None
