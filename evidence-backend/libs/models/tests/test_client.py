from __future__ import annotations

import httpx
import pytest

from models import GatewayError, GatewayUnavailableError, ModelGatewayClient


def _with_transport(client: ModelGatewayClient, handler: httpx.MockTransport) -> None:
    client._client = httpx.AsyncClient(  # noqa: SLF001 — test seam
        base_url="http://gateway", transport=handler
    )


async def test_embed_happy_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/embed"
        return httpx.Response(
            200, json={"vectors": [[0.1, 0.2]], "dim": 2, "role": "embed.dense", "model_id": "m"}
        )

    client = ModelGatewayClient("http://gateway")
    _with_transport(client, httpx.MockTransport(handler))
    assert await client.embed("embed.dense", ["text"]) == [[0.1, 0.2]]
    await client.aclose()


async def test_4xx_raises_gateway_error_immediately() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, json={"code": "unknown_role", "detail": "nope"})

    client = ModelGatewayClient("http://gateway", retries=3)
    _with_transport(client, httpx.MockTransport(handler))
    with pytest.raises(GatewayError) as excinfo:
        await client.embed("bogus", ["x"])
    assert excinfo.value.code == "unknown_role"
    assert calls == 1  # no retry on caller bugs
    await client.aclose()


async def test_5xx_retries_then_unavailable() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, text="boom")

    client = ModelGatewayClient("http://gateway", retries=2)
    _with_transport(client, httpx.MockTransport(handler))
    with pytest.raises(GatewayUnavailableError):
        await client.embed("embed.dense", ["x"])
    assert calls == 2
    await client.aclose()


async def test_5xx_then_success_recovers() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, text="warming up")
        return httpx.Response(200, json={"vectors": [[1.0]]})

    client = ModelGatewayClient("http://gateway", retries=3)
    _with_transport(client, httpx.MockTransport(handler))
    assert await client.embed("embed.dense", ["x"]) == [[1.0]]
    await client.aclose()
