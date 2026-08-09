"""Sprint 16 — generation pre-warm: a reachable-but-cold backend is unready."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Response
from generation_service.config import settings
from generation_service.routers.health import readyz


class _Inference:
    def __init__(self, *, up: bool = True) -> None:
        self._up = up

    async def ready(self) -> bool:
        return self._up


class _Redis:
    async def ping(self) -> bool:
        return True


def _request(state: SimpleNamespace | None) -> Any:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(svc=state)))


@pytest.fixture(autouse=True)
def _layer_c_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "layer_c_enabled", True)


async def test_warming_backend_reports_503(monkeypatch: pytest.MonkeyPatch) -> None:
    state = SimpleNamespace(redis=_Redis(), inference=_Inference(), warmed=False)
    resp = Response()
    body = await readyz(_request(state), resp)
    assert resp.status_code == 503
    assert body["reason"] == "model warming"


async def test_warmed_backend_reports_ready() -> None:
    state = SimpleNamespace(redis=_Redis(), inference=_Inference(), warmed=True)
    resp = Response()
    body = await readyz(_request(state), resp)
    assert resp.status_code == 200
    assert body["warmed"] is True


async def test_prewarm_disabled_states_are_ready_by_default() -> None:
    # build_state constructs warmed=True; the lifespan only flips it to
    # False when MDX_PREWARM_ENABLED — so the default posture is unchanged.
    state = SimpleNamespace(redis=_Redis(), inference=_Inference(), warmed=True)
    resp = Response()
    assert (await readyz(_request(state), resp))["status"] == "ready"
