"""Behavioural tests for ``GET /asr/prompts`` (M1·C1).

Exercises the real handler with the auth dependency overridden and the DB
boundary stubbed — no infra required (mirrors ``test_result_endpoint``).
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from auth import Claims


def _clinician_claims() -> Claims:
    return Claims(
        sub=uuid4(),
        tid=uuid4(),
        roles=["clinician"],
        sid="test-session",
        iss="https://test/issuer",
        aud="mdx",
        exp=9_999_999_999,
        iat=1_700_000_000,
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TESTING", "true")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    from asr_service import deps
    from asr_service.main import create_app
    from asr_service.routers import prompts

    deps.install_state(SimpleNamespace(app_pool=object()))  # type: ignore[arg-type]

    @contextlib.asynccontextmanager
    async def _fake_tenant_conn(pool, tenant_id):  # noqa: ANN001
        yield None

    monkeypatch.setattr(prompts, "tenant_connection", _fake_tenant_conn)

    app = create_app()
    app.dependency_overrides[deps.current_user] = _clinician_claims
    return TestClient(app)


def test_list_prompts_returns_catalogue(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from asr_service.domain.repository import PromptRow
    from asr_service.routers import prompts

    pid = uuid4()
    captured: dict = {}

    async def _list(conn, *, language=None, specialty=None):  # noqa: ANN001
        captured["language"] = language
        captured["specialty"] = specialty
        return [PromptRow(id=pid, language="uk", specialty="cardiology", is_default=True)]

    monkeypatch.setattr(prompts.repository, "list_prompts", _list)

    resp = client.get("/asr/prompts?language=uk")
    assert resp.status_code == 200
    body = resp.json()
    assert body == [
        {"id": str(pid), "language": "uk", "specialty": "cardiology", "is_default": True}
    ]
    assert captured == {"language": "uk", "specialty": None}


def test_list_prompts_rejects_bad_language(client: TestClient) -> None:
    # The ``^(uk|en)$`` pattern guard rejects unknown languages at the edge.
    assert client.get("/asr/prompts?language=de").status_code == 422
