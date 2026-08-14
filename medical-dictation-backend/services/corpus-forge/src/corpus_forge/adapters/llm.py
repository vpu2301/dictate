"""LLM clients for jury + generation (ADR-0044).

`LocalLlamaClient` / `LocalOllamaClient` talk to the same in-perimeter
backend that serves generation-service (sprint 15) — the ONLY judges
permitted for PHI-derived candidates. `ExternalAnthropicClient` is optional
and only ever sees public-data candidates; the routing raise lives in
domain/jury.py, not here.
"""

from __future__ import annotations

import httpx


class LocalLlamaClient:
    """llama-server native /completion (generation-service's default backend)."""

    in_perimeter = True

    def __init__(self, *, base_url: str, model: str, timeout_s: float = 60.0) -> None:
        self._model = model
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout_s)

    @property
    def model_name(self) -> str:
        return self._model

    async def complete(self, prompt: str) -> str:
        resp = await self._http.post(
            "/completion",
            json={"prompt": prompt, "n_predict": 512, "temperature": 0, "cache_prompt": True},
        )
        resp.raise_for_status()
        return str(resp.json().get("content", ""))

    async def aclose(self) -> None:
        await self._http.aclose()


class LocalOllamaClient:
    in_perimeter = True

    def __init__(self, *, base_url: str, model: str, timeout_s: float = 60.0) -> None:
        self._model = model
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout_s)

    @property
    def model_name(self) -> str:
        return self._model

    async def complete(self, prompt: str) -> str:
        resp = await self._http.post(
            "/api/generate",
            json={
                "model": self._model,
                "prompt": prompt,
                "stream": False,
                "options": {"num_predict": 512, "temperature": 0},
            },
        )
        resp.raise_for_status()
        return str(resp.json().get("response", ""))

    async def aclose(self) -> None:
        await self._http.aclose()


class ExternalAnthropicClient:
    """Anthropic Messages API. in_perimeter=False — domain/jury.py raises
    before this client ever sees a PHI-derived candidate."""

    in_perimeter = False

    def __init__(self, *, api_key: str, model: str, timeout_s: float = 60.0) -> None:
        self._model = model
        self._http = httpx.AsyncClient(
            base_url="https://api.anthropic.com",
            timeout=timeout_s,
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        )

    @property
    def model_name(self) -> str:
        return self._model

    async def complete(self, prompt: str) -> str:
        resp = await self._http.post(
            "/v1/messages",
            json={
                "model": self._model,
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        blocks = resp.json().get("content", [])
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")

    async def aclose(self) -> None:
        await self._http.aclose()


def build_local_client(*, backend: str, base_url: str, model: str) -> LocalLlamaClient | LocalOllamaClient:
    if backend == "ollama":
        return LocalOllamaClient(base_url=base_url, model=model)
    return LocalLlamaClient(base_url=base_url, model=model)
