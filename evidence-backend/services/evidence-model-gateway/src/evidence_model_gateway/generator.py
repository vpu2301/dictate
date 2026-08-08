"""Generation backends for the `generator.*` roles (EVA-S04).

Same swap-seam doctrine as the platform's generation-service `adapters/
inference.py`: the engine is a Protocol, changing backends is one env var.
Two real backends ship, both talking to a **self-hosted** server (rule LM1 —
there is no external-API backend and there never will be one):

* ``LlamaCppBackend`` — llama-server's native ``/completion``. Raw completion
  endpoint, so the Gemma instruct turn wrapper is applied here; without it a
  greedy gemma3 degenerates into repetition loops (the platform's S15 lesson).
  Constrained decode uses llama.cpp's ``json_schema`` parameter.
* ``OllamaBackend`` — ``/api/generate``; applies the model's chat template
  itself and takes a JSON schema via ``format``.

Budgets (temperature cap, token cap, timeout) are enforced HERE, not by
callers — rule LM2 says they are gateway-enforced configuration.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx

# Gemma 3 instruct turn format (llama.cpp raw-completion path only).
_GEMMA_TURN = "<start_of_turn>user\n{prompt}<end_of_turn>\n<start_of_turn>model\n"
_GEMMA_STOP = ["<end_of_turn>", "<start_of_turn>"]


class GenerationBackendError(Exception):
    """The backend could not serve the request (unreachable, HTTP error)."""


@dataclass(frozen=True, slots=True)
class GenerationResult:
    text: str
    model_id: str
    finish_reason: str = "stop"


@runtime_checkable
class GenerationBackend(Protocol):
    async def generate(
        self,
        *,
        prompt: str,
        max_tokens: int,
        temperature: float,
        stop: list[str] | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> GenerationResult: ...

    def stream(
        self,
        *,
        prompt: str,
        max_tokens: int,
        temperature: float,
        stop: list[str] | None = None,
    ) -> AsyncIterator[str]: ...

    async def ready(self) -> bool: ...

    async def aclose(self) -> None: ...


class LlamaCppBackend:
    def __init__(self, *, base_url: str, model_id: str, timeout_s: float) -> None:
        self._model_id = model_id
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout_s)

    def _payload(
        self,
        *,
        prompt: str,
        max_tokens: int,
        temperature: float,
        stop: list[str] | None,
        json_schema: dict[str, Any] | None,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "prompt": _GEMMA_TURN.format(prompt=prompt),
            "n_predict": max_tokens,
            "temperature": temperature,
            "stop": [*_GEMMA_STOP, *(stop or [])],
            "cache_prompt": True,
            "stream": stream,
        }
        if json_schema is not None:
            payload["json_schema"] = json_schema
        return payload

    async def generate(
        self,
        *,
        prompt: str,
        max_tokens: int,
        temperature: float,
        stop: list[str] | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> GenerationResult:
        try:
            response = await self._http.post(
                "/completion",
                json=self._payload(
                    prompt=prompt,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    stop=stop,
                    json_schema=json_schema,
                    stream=False,
                ),
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise GenerationBackendError(str(exc)) from exc
        body = response.json()
        return GenerationResult(
            text=str(body.get("content", "")),
            model_id=self._model_id,
            finish_reason="length" if body.get("truncated") else "stop",
        )

    async def stream(
        self,
        *,
        prompt: str,
        max_tokens: int,
        temperature: float,
        stop: list[str] | None = None,
    ) -> AsyncIterator[str]:
        payload = self._payload(
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            stop=stop,
            json_schema=None,
            stream=True,
        )
        try:
            async with self._http.stream("POST", "/completion", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    chunk = json.loads(line[5:].strip())
                    token = str(chunk.get("content", ""))
                    if token:
                        yield token
                    if chunk.get("stop"):
                        return
        except httpx.HTTPError as exc:
            raise GenerationBackendError(str(exc)) from exc

    async def ready(self) -> bool:
        try:
            response = await self._http.get("/health", timeout=2.0)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def aclose(self) -> None:
        await self._http.aclose()


class OllamaBackend:
    def __init__(self, *, base_url: str, model_id: str, timeout_s: float) -> None:
        self._model_id = model_id
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout_s)

    def _payload(
        self,
        *,
        prompt: str,
        max_tokens: int,
        temperature: float,
        stop: list[str] | None,
        json_schema: dict[str, Any] | None,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model_id,
            "prompt": prompt,
            "stream": stream,
            "options": {
                "num_predict": max_tokens,
                "temperature": temperature,
                "stop": stop or [],
            },
        }
        if json_schema is not None:
            payload["format"] = json_schema
        return payload

    async def generate(
        self,
        *,
        prompt: str,
        max_tokens: int,
        temperature: float,
        stop: list[str] | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> GenerationResult:
        try:
            response = await self._http.post(
                "/api/generate",
                json=self._payload(
                    prompt=prompt,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    stop=stop,
                    json_schema=json_schema,
                    stream=False,
                ),
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise GenerationBackendError(str(exc)) from exc
        body = response.json()
        return GenerationResult(
            text=str(body.get("response", "")),
            model_id=self._model_id,
            finish_reason=str(body.get("done_reason", "stop")),
        )

    async def stream(
        self,
        *,
        prompt: str,
        max_tokens: int,
        temperature: float,
        stop: list[str] | None = None,
    ) -> AsyncIterator[str]:
        payload = self._payload(
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            stop=stop,
            json_schema=None,
            stream=True,
        )
        try:
            async with self._http.stream("POST", "/api/generate", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    token = str(chunk.get("response", ""))
                    if token:
                        yield token
                    if chunk.get("done"):
                        return
        except httpx.HTTPError as exc:
            raise GenerationBackendError(str(exc)) from exc

    async def ready(self) -> bool:
        try:
            response = await self._http.get("/api/tags", timeout=2.0)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def aclose(self) -> None:
        await self._http.aclose()


def build_backend(
    *, backend: str, base_url: str, model_id: str, timeout_s: float
) -> GenerationBackend:
    if backend == "ollama":
        return OllamaBackend(base_url=base_url, model_id=model_id, timeout_s=timeout_s)
    return LlamaCppBackend(base_url=base_url, model_id=model_id, timeout_s=timeout_s)
