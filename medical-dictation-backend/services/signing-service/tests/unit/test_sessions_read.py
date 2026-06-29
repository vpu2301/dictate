"""Behavioural tests for the signing read endpoints (M1·B1/B3).

Real handlers, auth overridden, DB/registry stubbed — no infra.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from medical_kep import ProviderName

from auth import Claims


def _admin_claims() -> Claims:
    return Claims(
        sub=uuid4(),
        tid=uuid4(),
        roles=["tenant_admin"],
        sid="s",
        iss="https://test/issuer",
        aud="mdx",
        exp=9_999_999_999,
        iat=1_700_000_000,
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TESTING", "true")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    from signing_service import deps
    from signing_service.main import create_app
    from signing_service.routers import sessions

    deps.install_state(  # type: ignore[arg-type]
        SimpleNamespace(
            app_pool=object(),
            providers=SimpleNamespace(
                providers={ProviderName.IIT: object(), ProviderName.MOCK: object()}
            ),
        )
    )

    @contextlib.asynccontextmanager
    async def _fake_tenant_conn(pool, tenant_id):  # noqa: ANN001
        yield None

    monkeypatch.setattr(sessions, "tenant_connection", _fake_tenant_conn)

    app = create_app()
    app.dependency_overrides[deps.current_user] = _admin_claims
    return TestClient(app)


# ── B1: poll session ────────────────────────────────────────────────


def test_get_session_404_when_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from signing_service.routers import sessions

    async def _fetch(conn, *, session_id):  # noqa: ANN001
        return None

    monkeypatch.setattr(sessions.repo, "fetch_session_by_id", _fetch)
    assert client.get(f"/signing/sessions/{uuid4()}").status_code == 404


def test_get_session_awaiting_user(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from signing_service.routers import sessions

    sid = uuid4()

    async def _fetch(conn, *, session_id):  # noqa: ANN001
        return {
            "id": sid,
            "tenant_id": uuid4(),
            "status": "awaiting_user",
            "provider": "diia",
            "expires_at": datetime(2026, 6, 1, tzinfo=UTC),
            "redirect_url": "https://diia/sign",
            "qr_payload": None,
            "signed_envelope_id": None,
            "failure_reason": None,
            "verification_token": None,
            "signed_at": None,
            "signer_full_name": None,
        }

    monkeypatch.setattr(sessions.repo, "fetch_session_by_id", _fetch)
    resp = client.get(f"/signing/sessions/{sid}")
    assert resp.status_code == 200
    body = resp.json()
    # Backend enum returned verbatim (FE does the mapping).
    assert body["status"] == "awaiting_user"
    assert body["redirect_url"] == "https://diia/sign"
    assert body["verification_token"] is None


def test_get_session_signed_surfaces_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from signing_service.routers import sessions

    sid, env_id = uuid4(), uuid4()

    async def _fetch(conn, *, session_id):  # noqa: ANN001
        return {
            "id": sid,
            "tenant_id": uuid4(),
            "status": "signed",
            "provider": "iit",
            "expires_at": datetime(2026, 6, 1, tzinfo=UTC),
            "redirect_url": None,
            "qr_payload": None,
            "signed_envelope_id": env_id,
            "failure_reason": None,
            "verification_token": "vtok-abc",
            "signed_at": datetime(2026, 5, 30, tzinfo=UTC),
            "signer_full_name": "Dr Test",
        }

    monkeypatch.setattr(sessions.repo, "fetch_session_by_id", _fetch)
    resp = client.get(f"/signing/sessions/{sid}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "signed"
    assert body["signed_envelope_id"] == str(env_id)
    assert body["verification_token"] == "vtok-abc"
    assert body["signer_full_name"] == "Dr Test"
    assert body["signed_at"].startswith("2026-05-30")


# ── B2: cancel session ──────────────────────────────────────────────


def _session_row(status: str) -> dict:
    return {
        "id": uuid4(),
        "tenant_id": uuid4(),
        "status": status,
        "provider": "diia",
        "expires_at": datetime(2026, 6, 1, tzinfo=UTC),
        "redirect_url": None,
        "qr_payload": None,
        "signed_envelope_id": None,
        "failure_reason": None,
        "verification_token": None,
        "signed_at": None,
        "signer_full_name": None,
    }


def test_cancel_404_when_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from signing_service.routers import sessions

    async def _fetch(conn, *, session_id):  # noqa: ANN001
        return None

    monkeypatch.setattr(sessions.repo, "fetch_session_by_id", _fetch)
    assert client.delete(f"/signing/sessions/{uuid4()}").status_code == 404


def test_cancel_409_when_verifying(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from signing_service.routers import sessions

    async def _fetch(conn, *, session_id):  # noqa: ANN001
        return _session_row("verifying")

    monkeypatch.setattr(sessions.repo, "fetch_session_by_id", _fetch)
    resp = client.delete(f"/signing/sessions/{uuid4()}")
    assert resp.status_code == 409
    assert "session-not-cancellable" in resp.text


def test_cancel_ok_from_awaiting_user(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from signing_service.routers import sessions

    captured: dict = {}

    async def _fetch(conn, *, session_id):  # noqa: ANN001
        return _session_row("awaiting_user")

    async def _transition(conn, *, session_id, expected_from, to, failure_reason=None):  # noqa: ANN001
        captured["expected_from"] = expected_from
        captured["to"] = to
        captured["failure_reason"] = failure_reason
        return True

    audit: list[dict] = []

    async def _write_event(**kwargs):  # noqa: ANN003
        audit.append(kwargs)

    from signing_service import deps

    deps.get_state().audit_writer = SimpleNamespace(write_event=_write_event)  # type: ignore[attr-defined]
    monkeypatch.setattr(sessions.repo, "fetch_session_by_id", _fetch)
    monkeypatch.setattr(sessions.repo, "transition_session", _transition)

    resp = client.delete(f"/signing/sessions/{uuid4()}")
    assert resp.status_code == 200
    assert resp.json() == {"status": "cancelled"}
    assert captured == {
        "expected_from": "awaiting_user",
        "to": "cancelled",
        "failure_reason": "user_cancelled",
    }
    assert len(audit) == 1
    assert audit[0]["kind"] == "signing.session.cancelled"


def test_cancel_409_on_race(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from signing_service.routers import sessions

    async def _fetch(conn, *, session_id):  # noqa: ANN001
        return _session_row("awaiting_user")

    async def _transition(conn, *, session_id, expected_from, to, failure_reason=None):  # noqa: ANN001
        return False  # reaper moved it between fetch and update

    monkeypatch.setattr(sessions.repo, "fetch_session_by_id", _fetch)
    monkeypatch.setattr(sessions.repo, "transition_session", _transition)
    assert client.delete(f"/signing/sessions/{uuid4()}").status_code == 409


# ── B3: certificates ────────────────────────────────────────────────


def test_list_certificates_returns_local_only(client: TestClient) -> None:
    resp = client.get("/signing/certificates")
    assert resp.status_code == 200
    body = resp.json()
    # Only the local ІІТ provider is a "local cert"; MOCK is filtered out.
    assert body == [{
        "provider": "iit",
        "subject_cn": None,
        "issuer_cn": None,
        "serial": None,
        "valid_from": None,
        "valid_to": None,
        "is_qualified": True,
    }]
