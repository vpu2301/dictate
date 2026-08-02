"""Inference backend seam (ADR-0036).

Same swap-seam doctrine as report-service ``domain/synthesis.py``: the
engine is a Protocol; changing backends is one env var, not a refactor.
Two real backends ship:

* ``LlamaCppClient`` — llama-server native ``/completion``. The default:
  no per-request scheduler overhead (measured ~420 ms/request in Ollama
  0.32.5 with gemma3). llama-server serves a raw completion endpoint, so
  the Gemma chat-turn wrapper is applied HERE — without it greedy gemma3
  degenerates into repetition loops.
* ``OllamaClient`` — Ollama ``/api/generate`` (applies the model's chat
  template itself). Kept as the operationally simpler alternative.

The deterministic mock used by tests lives in ``tests/``, not here — no
mock logic ships in the service (sprint-15 delivery mandate).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import httpx

from .. import config

# Gemma 3 instruct turn format (llama.cpp raw-completion path only).
_GEMMA_TURN = "<start_of_turn>user\n{prompt}<end_of_turn>\n<start_of_turn>model\n"
# `\n\n` stops multi-paragraph rambles: an inline completion finishes the
# current sentence, it never starts a new paragraph.
_STOP = ["<end_of_turn>", "\n\n"]


@dataclass(slots=True, frozen=True)
class CompletionResult:
    text: str
    model: str


@runtime_checkable
class InferenceClient(Protocol):
    async def complete(self, *, prompt: str, max_tokens: int) -> CompletionResult: ...

    async def ready(self) -> bool: ...

    async def aclose(self) -> None: ...


class LlamaCppClient:
    def __init__(self, *, base_url: str, model: str) -> None:
        self._model = model
        self._http = httpx.AsyncClient(base_url=base_url, timeout=30.0)

    async def complete(self, *, prompt: str, max_tokens: int) -> CompletionResult:
        resp = await self._http.post(
            "/completion",
            json={
                "prompt": _GEMMA_TURN.format(prompt=prompt),
                "n_predict": max_tokens,
                "temperature": 0,
                "stop": _STOP,
                "cache_prompt": True,
            },
        )
        resp.raise_for_status()
        return CompletionResult(
            text=resp.json().get("content", ""), model=self._model
        )

    async def ready(self) -> bool:
        try:
            resp = await self._http.get("/health", timeout=2.0)
        except httpx.HTTPError:
            return False
        return resp.status_code == 200

    async def aclose(self) -> None:
        await self._http.aclose()


class OllamaClient:
    def __init__(self, *, base_url: str, model: str) -> None:
        self._model = model
        self._http = httpx.AsyncClient(base_url=base_url, timeout=30.0)

    async def complete(self, *, prompt: str, max_tokens: int) -> CompletionResult:
        resp = await self._http.post(
            "/api/generate",
            json={
                "model": self._model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "num_predict": max_tokens,
                    "temperature": 0,
                    "stop": _STOP,
                },
            },
        )
        resp.raise_for_status()
        return CompletionResult(
            text=resp.json().get("response", ""), model=self._model
        )

    async def ready(self) -> bool:
        try:
            resp = await self._http.get("/api/tags", timeout=2.0)
        except httpx.HTTPError:
            return False
        return resp.status_code == 200

    async def aclose(self) -> None:
        await self._http.aclose()


def build_inference_client(settings: config.Settings) -> InferenceClient:
    if settings.gen_backend == "ollama":
        return OllamaClient(base_url=settings.gen_base_url, model=settings.gen_model)
    return LlamaCppClient(base_url=settings.gen_base_url, model=settings.gen_model)
