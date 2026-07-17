"""S11 step 06 — DSAR API surface (scope, kick-off, status, download)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from tests.conftest import REQUESTER_SUB, TENANT_ID

PATIENT_ID = UUID("33333333-3333-3333-3333-333333333333")
REQUEST_ID = UUID("99999999-9999-9999-9999-999999999999")
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def _dsar_row(**over: object) -> dict:
    base: dict = {
        "id": REQUEST_ID,
        "tenant_id": TENANT_ID,
        "patient_id": PATIENT_ID,
        "kind": "dsar",
        "reason": "",
        "status": "requested",
        "requested_by": REQUESTER_SUB,
        "requested_at": NOW,
        "scheduled_for": None,
        "reviewed_by": None,
        "reviewed_at": None,
        "rejection_reason": None,
        "executing_at": None,
        "completed_at": None,
        "report_of_execution": None,
        "package_object_key": None,
        "package_deleted_at": None,
    }
    base.update(over)
    return base


def _token(request_id: UUID = REQUEST_ID, ttl_seconds: int = 900) -> str:
    """A valid download token for the test tenant (ADR-0028)."""
    from core_service.config import settings
    from core_service.domain import download_tokens

    token, _ = download_tokens.mint(
        settings.dsar_download_token_hmac_key_hex,
        tenant_id=TENANT_ID,
        request_id=request_id,
        ttl_seconds=ttl_seconds,
    )
    return token


def _stub_repo(monkeypatch: pytest.MonkeyPatch, name: str, result) -> dict:
    from core_service.domain import privacy_repository

    captured: dict = {}

    async def _fn(conn, **kwargs):  # noqa: ANN001, ANN003
        captured.update(kwargs)
        return result

    monkeypatch.setattr(privacy_repository, name, _fn)
    return captured


def _stub_engine(monkeypatch: pytest.MonkeyPatch) -> dict:
    from core_service.erasure import dsar as engine

    calls: dict = {}

    async def _run(state, **kwargs):  # noqa: ANN001, ANN003
        calls.update(kwargs)

    monkeypatch.setattr(engine, "run_export", _run)
    return calls


def _admin(make_client: Callable[[list[str]], TestClient]) -> TestClient:
    return make_client(["tenant_admin"])


# ── scope ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["clinician", "nurse", "auditor"])
def test_dsar_requires_dedicated_scope(
    make_client: Callable[[list[str]], TestClient], role: str
) -> None:
    """Tightened from patient.write to patient.dsar (tenant_admin only)."""
    resp = make_client([role]).post(f"/patients/{PATIENT_ID}/dsar", json={})
    assert resp.status_code == 403, resp.text


# ── kick-off ────────────────────────────────────────────────────────


def _stub_patient(monkeypatch: pytest.MonkeyPatch, status_: str = "active") -> None:
    from core_service.domain import patients_repository

    async def _get(conn, *, patient_id):  # noqa: ANN001
        return {"id": patient_id, "status": status_}

    monkeypatch.setattr(patients_repository, "get_patient", _get)


def test_dsar_kickoff_202_and_task_spawned(
    make_client: Callable[[list[str]], TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = _admin(make_client)
    _stub_patient(monkeypatch)
    _stub_repo(monkeypatch, "find_active_dsar", None)
    _stub_repo(monkeypatch, "create_request", _dsar_row())
    _stub_repo(monkeypatch, "mark_executing", _dsar_row(status="executing", executing_at=NOW))
    calls = _stub_engine(monkeypatch)

    resp = admin.post(f"/patients/{PATIENT_ID}/dsar", json={"reason": "art 15"})
    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "executing"
    assert calls["request_id"] == REQUEST_ID  # background task got the row
    kinds = [c["kind"] for c in admin.audit_calls]  # type: ignore[attr-defined]
    assert "privacy.dsar_requested" in kinds


def test_dsar_on_erased_patient_409(
    make_client: Callable[[list[str]], TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = _admin(make_client)
    _stub_patient(monkeypatch, status_="erased")
    resp = admin.post(f"/patients/{PATIENT_ID}/dsar", json={})
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "patient_erased"


def test_dsar_second_post_while_running_409_with_id(
    make_client: Callable[[list[str]], TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = _admin(make_client)
    _stub_patient(monkeypatch)
    _stub_repo(
        monkeypatch, "find_active_dsar",
        _dsar_row(status="executing", executing_at=NOW),
    )
    _stub_repo(monkeypatch, "revert_stale_executing", None)  # fresh — no takeover

    resp = admin.post(f"/patients/{PATIENT_ID}/dsar", json={})
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["code"] == "dsar_already_running"
    assert body["request_id"] == str(REQUEST_ID)


def test_dsar_stale_executing_taken_over(
    make_client: Callable[[list[str]], TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = _admin(make_client)
    _stub_patient(monkeypatch)
    stale = _dsar_row(status="executing", executing_at=NOW - timedelta(hours=2))
    _stub_repo(monkeypatch, "find_active_dsar", stale)
    _stub_repo(monkeypatch, "revert_stale_executing", _dsar_row(status="requested"))
    _stub_repo(monkeypatch, "mark_executing", _dsar_row(status="executing"))
    calls = _stub_engine(monkeypatch)

    resp = admin.post(f"/patients/{PATIENT_ID}/dsar", json={})
    assert resp.status_code == 202, resp.text
    assert calls["request_id"] == REQUEST_ID  # re-run, not a new request


# ── status + download ───────────────────────────────────────────────


def test_status_completed_mints_audited_download(
    make_client: Callable[[list[str]], TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = _admin(make_client)
    _stub_repo(
        monkeypatch, "get_request",
        _dsar_row(
            status="completed",
            completed_at=NOW,
            package_object_key=f"dsar/{TENANT_ID}/{REQUEST_ID}.zip",
            report_of_execution='{"item_count": 5}',
        ),
    )
    resp = admin.get(f"/privacy-requests/{REQUEST_ID}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # ADR-0028: the link carries a short-lived HMAC token; expires_at
    # reflects the 15-minute link TTL, not the package TTL.
    assert f"/privacy-requests/{REQUEST_ID}/download?t=" in body["download"]["url"]
    expires = datetime.fromisoformat(body["download"]["expires_at"])
    assert expires - datetime.now(UTC) <= timedelta(minutes=15)
    assert body["manifest_summary"]["item_count"] == 5
    assert not body["package_expired"]
    kinds = [c["kind"] for c in admin.audit_calls]  # type: ignore[attr-defined]
    assert "dsar.download.link_issued" in kinds


def test_status_after_ttl_delete_shows_expired(
    make_client: Callable[[list[str]], TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = _admin(make_client)
    _stub_repo(
        monkeypatch, "get_request",
        _dsar_row(status="completed", completed_at=NOW, package_deleted_at=NOW),
    )
    body = admin.get(f"/privacy-requests/{REQUEST_ID}").json()
    assert body["package_expired"] is True
    assert body["download"] is None


def test_download_after_ttl_delete_410(
    make_client: Callable[[list[str]], TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = _admin(make_client)
    _stub_repo(
        monkeypatch, "get_request",
        _dsar_row(status="completed", completed_at=NOW, package_deleted_at=NOW),
    )
    resp = admin.get(f"/privacy-requests/{REQUEST_ID}/download?t={_token()}")
    assert resp.status_code == 410, resp.text
    assert resp.json()["code"] == "package_expired"


def test_download_streams_zip_and_audits(
    make_client: Callable[[list[str]], TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    from core_service.erasure import dsar as engine

    admin = _admin(make_client)
    key = f"dsar/{TENANT_ID}/{REQUEST_ID}.zip"
    _stub_repo(
        monkeypatch, "get_request",
        _dsar_row(status="completed", completed_at=NOW, package_object_key=key),
    )

    class _FakeStore:
        async def get(self, *, key, tenant_id, aad):  # noqa: ANN001
            assert aad == REQUEST_ID.bytes
            return b"PK-fake-zip"

    async def _runtime(state):  # noqa: ANN001
        return SimpleNamespace(dsar_store=_FakeStore())

    monkeypatch.setattr(engine, "get_runtime", _runtime)

    resp = admin.get(f"/privacy-requests/{REQUEST_ID}/download?t={_token()}")
    assert resp.status_code == 200, resp.text
    assert resp.content == b"PK-fake-zip"
    assert resp.headers["content-type"] == "application/zip"
    kinds = [c["kind"] for c in admin.audit_calls]  # type: ignore[attr-defined]
    assert "dsar.package.downloaded" in kinds


# ── download-link token (S11 deployment, ADR-0028) ──────────────────


@pytest.mark.parametrize(
    "bad_token",
    ["", "garbage", "123.deadbeef", "notanint.abc"],
    ids=["missing", "garbage", "wrong-mac", "malformed-exp"],
)
def test_download_without_valid_token_403(
    make_client: Callable[[list[str]], TestClient],
    monkeypatch: pytest.MonkeyPatch,
    bad_token: str,
) -> None:
    admin = _admin(make_client)
    _stub_repo(
        monkeypatch, "get_request",
        _dsar_row(
            status="completed", completed_at=NOW,
            package_object_key=f"dsar/{TENANT_ID}/{REQUEST_ID}.zip",
        ),
    )
    resp = admin.get(f"/privacy-requests/{REQUEST_ID}/download?t={bad_token}")
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "download_link_expired"


def test_download_expired_token_403(
    make_client: Callable[[list[str]], TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = _admin(make_client)
    resp = admin.get(
        f"/privacy-requests/{REQUEST_ID}/download?t={_token(ttl_seconds=-1)}"
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "download_link_expired"


def test_download_token_bound_to_request_403(
    make_client: Callable[[list[str]], TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    other = UUID("88888888-8888-8888-8888-888888888888")
    admin = _admin(make_client)
    resp = admin.get(
        f"/privacy-requests/{REQUEST_ID}/download?t={_token(request_id=other)}"
    )
    assert resp.status_code == 403, resp.text
