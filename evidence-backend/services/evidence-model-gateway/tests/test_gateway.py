from __future__ import annotations

import os

os.environ.setdefault("EVA_GATEWAY_TESTING", "true")

from evidence_model_gateway.config import Settings  # noqa: E402
from evidence_model_gateway.main import create_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class FakeModel:
    def encode(self, texts: list[str], **_: object) -> list[list[float]]:
        return [[float(len(t)), 0.5] for t in texts]


def _client(**overrides: object) -> TestClient:
    settings = Settings(testing=True, embed_dim=2, **overrides)  # type: ignore[arg-type]
    app = create_app(settings)
    return TestClient(app)


def test_healthz() -> None:
    client = _client()
    assert client.get("/healthz").json()["status"] == "ok"


def test_readyz_503_until_model_loaded() -> None:
    client = _client()
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["code"] == "model_not_loaded"


def test_roles_lists_registered_roles() -> None:
    client = _client()
    roles = {r["name"]: r for r in client.get("/roles").json()}
    assert set(roles) == {"embed.dense", "rerank", "generator.fast", "generator.heavy"}
    assert roles["embed.dense"]["kind"] == "embedding"
    assert roles["rerank"]["kind"] == "rerank"
    assert roles["generator.fast"]["kind"] == "generation"
    assert roles["generator.heavy"]["kind"] == "generation"


def test_rerank_unknown_role_404() -> None:
    client = _client()
    response = client.post("/v1/rerank", json={"role": "embed.dense", "query": "q", "texts": ["x"]})
    assert response.status_code == 404


def test_rerank_disabled_gateway_has_no_role() -> None:
    client = _client(rerank_enabled=False, generation_enabled=False)
    roles = [r["name"] for r in client.get("/roles").json()]
    assert roles == ["embed.dense"]


def test_generation_disabled_gateway_has_no_generator_roles() -> None:
    """An embed/rerank-only gateway for retrieval-side work (S03 flow)."""
    client = _client(generation_enabled=False)
    roles = [r["name"] for r in client.get("/roles").json()]
    assert roles == ["embed.dense", "rerank"]


def test_generate_unknown_role_404() -> None:
    client = _client()
    response = client.post("/v1/generate", json={"role": "embed.dense", "prompt": "hi"})
    assert response.status_code == 404
    assert response.json()["code"] == "unknown_role"


def test_generate_rejects_an_oversized_prompt() -> None:
    client = _client(max_prompt_chars=10)
    response = client.post("/v1/generate", json={"role": "generator.fast", "prompt": "x" * 50})
    assert response.status_code == 422


def test_generate_surfaces_a_dead_backend_as_503_not_a_crash() -> None:
    """No llama-server in CI: the honest answer is `gateway_unavailable`,
    which is what makes the answer pipeline fail closed (rule CS3)."""
    client = _client(gen_fast_base_url="http://127.0.0.1:9")
    response = client.post(
        "/v1/generate", json={"role": "generator.fast", "prompt": "hi", "max_tokens": 8}
    )
    assert response.status_code == 503


def test_generation_roles_gate_readiness_when_enabled() -> None:
    """Rule BE6: readiness asserts the serving stack. A registered role whose
    backend is unreachable means the gateway cannot serve what it advertises."""
    client = _client(gen_fast_base_url="http://127.0.0.1:9")
    response = client.get("/readyz")
    assert response.status_code == 503


def test_embed_unknown_role_404() -> None:
    client = _client()
    response = client.post("/v1/embed", json={"role": "generator.fast", "texts": ["x"]})
    assert response.status_code == 404


def test_embed_caps_enforced() -> None:
    client = _client(max_texts_per_request=2)
    response = client.post("/v1/embed", json={"role": "embed.dense", "texts": ["a", "b", "c"]})
    assert response.status_code == 422


def test_embed_happy_path_with_fake_model() -> None:
    client = _client()
    client.app.state.embedder._model = FakeModel()  # type: ignore[union-attr]
    response = client.post("/v1/embed", json={"role": "embed.dense", "texts": ["hello", "hi"]})
    assert response.status_code == 200
    body = response.json()
    assert body["vectors"] == [[5.0, 0.5], [2.0, 0.5]]
    assert body["dim"] == 2


def test_service_token_enforced_when_configured() -> None:
    client = _client(service_token="sekret")
    assert client.get("/roles").status_code == 401
    ok = client.get("/roles", headers={"Authorization": "Bearer sekret"})
    assert ok.status_code == 200


def test_extra_fields_rejected() -> None:
    client = _client()
    response = client.post(
        "/v1/embed", json={"role": "embed.dense", "texts": ["x"], "model": "sneaky"}
    )
    assert response.status_code == 422
