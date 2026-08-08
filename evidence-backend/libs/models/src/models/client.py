from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx


class GatewayError(Exception):
    """Gateway answered with a problem (4xx/5xx after retries)."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(f"gateway {status} {code}: {detail}")
        self.status = status
        self.code = code
        self.detail = detail


class GatewayUnavailableError(GatewayError):
    """Gateway unreachable / not ready — callers fail closed (rule CS3)."""

    def __init__(self, detail: str) -> None:
        super().__init__(503, "gateway_unavailable", detail)


@dataclass(frozen=True, slots=True)
class GenerationResult:
    text: str
    model_id: str
    finish_reason: str = "stop"
    # Temperature the gateway actually used after its LM2 clamp.
    temperature: float = 0.0


class ModelGatewayClient:
    """Async client for evidence-model-gateway.

    Retries transient failures (connect errors, 5xx) with exponential backoff;
    4xx are surfaced immediately — they are caller bugs, not weather.
    """

    def __init__(
        self,
        base_url: str,
        *,
        service_token: str | None = None,
        timeout_s: float = 120.0,
        retries: int = 3,
    ) -> None:
        headers = {}
        if service_token:
            headers["Authorization"] = f"Bearer {service_token}"
        self._client = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout_s)
        self._retries = retries

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        last_error = "no attempt made"
        for attempt in range(self._retries):
            try:
                response = await self._client.post(path, json=payload)
            except httpx.HTTPError as exc:
                last_error = str(exc)
            else:
                if response.status_code < 500:
                    if response.status_code >= 400:
                        body = response.json() if response.content else {}
                        raise GatewayError(
                            response.status_code,
                            str(body.get("code", "error")),
                            str(body.get("detail", response.text)),
                        )
                    return dict(response.json())
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
            if attempt < self._retries - 1:
                await asyncio.sleep(2**attempt)
        raise GatewayUnavailableError(last_error)

    async def embed(self, role: str, texts: list[str]) -> list[list[float]]:
        """Embed texts under a gateway role (e.g. 'embed.dense')."""
        data = await self._post("/v1/embed", {"role": role, "texts": texts})
        return [list(map(float, v)) for v in data["vectors"]]

    async def generate(
        self,
        role: str,
        prompt: str,
        *,
        max_tokens: int = 512,
        temperature: float = 0.0,
        stop: list[str] | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> GenerationResult:
        """Generate under a gateway role (`generator.fast` / `generator.heavy`).

        The gateway clamps `temperature` to its LM2 ceiling and echoes back
        what it used, so a caller can record the real value in provenance.
        """
        payload: dict[str, Any] = {
            "role": role,
            "prompt": prompt,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if stop is not None:
            payload["stop"] = stop
        if json_schema is not None:
            payload["json_schema"] = json_schema
        data = await self._post("/v1/generate", payload)
        return GenerationResult(
            text=str(data["text"]),
            model_id=str(data["model_id"]),
            finish_reason=str(data.get("finish_reason", "stop")),
            temperature=float(data.get("temperature", temperature)),
        )

    async def generate_stream(
        self,
        role: str,
        prompt: str,
        *,
        max_tokens: int = 512,
        temperature: float = 0.0,
        stop: list[str] | None = None,
    ) -> AsyncIterator[str]:
        """Token stream for the same roles. Not retried: a partially consumed
        stream cannot be replayed transparently, and the answer pipeline has
        its own degrade path (an aborted synthesis becomes `insufficient_basis`
        or closes with what already streamed).
        """
        payload: dict[str, Any] = {
            "role": role,
            "prompt": prompt,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if stop is not None:
            payload["stop"] = stop
        try:
            async with self._client.stream("POST", "/v1/generate/stream", json=payload) as response:
                if response.status_code >= 400:
                    await response.aread()
                    body = response.json() if response.content else {}
                    raise GatewayError(
                        response.status_code,
                        str(body.get("code", "error")),
                        str(body.get("detail", "generation stream failed")),
                    )
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    frame = json.loads(line[5:].strip())
                    if "error" in frame:
                        raise GatewayUnavailableError(str(frame["error"]))
                    if frame.get("done"):
                        return
                    token = str(frame.get("token", ""))
                    if token:
                        yield token
        except httpx.HTTPError as exc:
            raise GatewayUnavailableError(str(exc)) from exc

    async def rerank(self, role: str, query: str, texts: list[str]) -> list[float]:
        """Cross-encoder scores (raw logits) for (query, text) pairs."""
        data = await self._post("/v1/rerank", {"role": role, "query": query, "texts": texts})
        return [float(s) for s in data["scores"]]

    async def roles(self) -> list[dict[str, Any]]:
        response = await self._client.get("/roles")
        response.raise_for_status()
        return list(response.json())

    async def ready(self) -> bool:
        try:
            response = await self._client.get("/readyz")
        except httpx.HTTPError:
            return False
        return response.status_code == 200
