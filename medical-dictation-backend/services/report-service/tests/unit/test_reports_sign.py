"""POST /v1/reports/{id}/sign (S09 revision) — the sign surface.

The signing-service HTTP hop is stubbed (``_post_signing``); these
assert the 409 gate, the canonical-binding payload it delegates, the
provider request validation, error pass-through, and amendment routing.
"""

from __future__ import annotations

import contextlib
import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from audit.canonical import canonicalize
from auth import Claims
from report_models import ReportContent, ReportStatus


class _FakeRedis:
    """Records XADDs so a test can assert the sprint-12 event was emitted.

    Mirrors the real ServiceState, which now carries a Redis client for
    the notification event bus.
    """

    def __init__(self) -> None:
        self.xadds: list[tuple[str, dict]] = []

    async def xadd(self, name, fields, **kwargs):  # noqa: ANN001, ANN003
        self.xadds.append((name, fields))
        return b"0-1"

REQUESTER_SUB = UUID("11111111-1111-1111-1111-111111111111")
REPORT_ID = UUID("33333333-3333-3333-3333-333333333333")
TEMPLATE_ID = UUID("44444444-4444-4444-4444-444444444444")


def _clinician_claims() -> Claims:
    return Claims(
        sub=REQUESTER_SUB,
        tid=uuid4(),
        roles=["clinician"],
        sid="s",
        iss="https://test/issuer",
        aud="mdx",
        exp=9_999_999_999,
        iat=1_700_000_000,
        preferred_username="clinician@tenant-a.example",
        name="Лікар Тестовий",
    )


def _report_row(*, status: ReportStatus):
    from report_service.domain.reports_repository import ReportRow

    now = datetime(2026, 5, 20, tzinfo=UTC)
    return ReportRow(
        id=REPORT_ID,
        tenant_id=uuid4(),
        code="REP-2026-00001",
        status=status,
        current_version_id=uuid4(),
        current_version_number=1,
        primary_author_id=REQUESTER_SUB,
        co_author_ids=[],
        title="Chest CT",
        icd10_codes=["G43.0"],
        encounter_date=now,
        created_at=now,
        updated_at=now,
        finalized_at=now,
        signed_at=None,
        cancelled_at=None,
    )


def _version_row(*, is_amendment: bool = False, signed_at=None):
    from report_service.domain.reports_repository import VersionRow

    return VersionRow(
        id=uuid4(),
        report_id=REPORT_ID,
        version_number=2 if is_amendment else 1,
        parent_version_id=None,
        created_by=REQUESTER_SUB,
        created_at=datetime(2026, 5, 20, tzinfo=UTC),
        content=ReportContent(template_id=TEMPLATE_ID, template_schema_version=1),
        rendered_text="body",
        body_hash=None,
        is_amendment=is_amendment,
        amendment_type=None,
        amendment_reason=None,
        signed_at=signed_at,
        signed_by=None,
    )


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TESTING", "true")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    from report_service import deps
    from report_service.main import create_app
    from report_service.routers import reports_sign

    audit_calls: list[dict] = []

    async def _write_event(**kwargs):
        audit_calls.append(kwargs)

    deps.install_state(  # type: ignore[arg-type]
        SimpleNamespace(
            app_pool=object(),
            audit_writer=SimpleNamespace(write_event=_write_event),
            redis=_FakeRedis(),
        )
    )

    @contextlib.asynccontextmanager
    async def _fake_tenant_conn(pool, tenant_id):
        yield None

    monkeypatch.setattr(reports_sign, "tenant_connection", _fake_tenant_conn)

    async def _branding(conn, *, tenant_id):
        return SimpleNamespace(issuer_name="Клініка Тест")

    monkeypatch.setattr(reports_sign, "load_tenant_branding", _branding)

    posts: list[dict] = []

    def install_signing_response(result: dict | Exception):
        async def _post(path, body, *, auth_header, expected):
            posts.append({"path": path, "body": body, "auth": auth_header})
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(reports_sign, "_post_signing", _post)

    app = create_app()
    app.dependency_overrides[deps.current_user] = _clinician_claims
    client = TestClient(app)
    return SimpleNamespace(
        client=client,
        audit=audit_calls,
        posts=posts,
        install=install_signing_response,
        module=reports_sign,
        monkeypatch=monkeypatch,
    )


def _stub_report(harness, *, status: ReportStatus, version=None):
    async def _lock(conn, *, report_id):
        return _report_row(status=status) if report_id == REPORT_ID else None

    async def _fetch_version(conn, *, version_id):
        return version or _version_row()

    harness.monkeypatch.setattr(harness.module.repo, "lock_report_for_update", _lock)
    harness.monkeypatch.setattr(harness.module.repo, "fetch_version", _fetch_version)


def _inline_result() -> dict:
    return {
        "envelope_id": str(uuid4()),
        "session_id": str(uuid4()),
        "signature_level": "dev",
        "verification_token": "tok_x",
        "signed_at": "2026-07-03T12:00:00+00:00",
        "signer_full_name": "Лікар Тестовий",
        "is_qualified": False,
        "report_status": "signed",
    }


def test_sign_404_when_missing(harness) -> None:
    async def _lock(conn, *, report_id):
        return None

    harness.monkeypatch.setattr(harness.module.repo, "lock_report_for_update", _lock)
    resp = harness.client.post(
        f"/v1/reports/{uuid4()}/sign", json={"provider": "dev_password", "password": "x"}
    )
    assert resp.status_code == 404


def test_sign_409_when_draft(harness) -> None:
    _stub_report(harness, status=ReportStatus.DRAFT)
    resp = harness.client.post(
        f"/v1/reports/{REPORT_ID}/sign",
        json={"provider": "dev_password", "password": "x"},
    )
    assert resp.status_code == 409
    assert "report_not_signable" in resp.text


def test_sign_409_when_already_signed_without_amendment(harness) -> None:
    _stub_report(harness, status=ReportStatus.SIGNED)  # current version not an amendment
    resp = harness.client.post(
        f"/v1/reports/{REPORT_ID}/sign",
        json={"provider": "dev_password", "password": "x"},
    )
    assert resp.status_code == 409


def test_dev_password_delegates_inline_with_canonical_binding(harness) -> None:
    _stub_report(harness, status=ReportStatus.FINALIZED)
    harness.install(_inline_result())
    resp = harness.client.post(
        f"/v1/reports/{REPORT_ID}/sign",
        json={"provider": "dev_password", "password": "dev-password"},
        headers={"Authorization": "Bearer user-jwt"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["signature_level"] == "dev"
    assert resp.json()["report_status"] == "signed"

    post = harness.posts[0]
    assert post["path"] == "/signing/inline"
    assert post["auth"] == "Bearer user-jwt"  # JWT forwarded verbatim
    body = post["body"]
    assert body["provider"] == "dev_password"
    assert body["resource_type"] == "report"
    # The hash it sends really is the JCS hash of the canonical object.
    recomputed = hashlib.sha256(canonicalize(body["canonical_json"])).hexdigest()
    assert body["canonical_hash_hex"] == recomputed
    assert body["canonical_json"]["canonical_version"] == "1.0"
    assert body["canonical_json"]["report"]["code"] == "REP-2026-00001"

    kinds = [a["kind"] for a in harness.audit]
    assert "report.sign_requested" in kinds


def test_file_key_requires_container_and_password(harness) -> None:
    _stub_report(harness, status=ReportStatus.FINALIZED)
    harness.install(_inline_result())
    resp = harness.client.post(
        f"/v1/reports/{REPORT_ID}/sign", json={"provider": "file_key"}
    )
    assert resp.status_code == 400
    assert "missing_credentials" in resp.text


def test_file_key_delegates_credentials(harness) -> None:
    _stub_report(harness, status=ReportStatus.FINALIZED)
    result = _inline_result() | {"signature_level": "qualified", "is_qualified": True}
    harness.install(result)
    resp = harness.client.post(
        f"/v1/reports/{REPORT_ID}/sign",
        json={
            "provider": "file_key",
            "key_container_b64": "MIIB",
            "key_password": "pw",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["signature_level"] == "qualified"
    assert harness.posts[0]["body"]["key_container_b64"] == "MIIB"


def test_diia_returns_202_with_session(harness) -> None:
    _stub_report(harness, status=ReportStatus.FINALIZED)
    harness.install(
        {
            "session_id": str(uuid4()),
            "provider": "diia",
            "expires_at": "2026-07-03T12:01:00+00:00",
            "redirect_url": "https://diia.example/sign/x",
            "qr_payload": "diia://x",
        }
    )
    resp = harness.client.post(
        f"/v1/reports/{REPORT_ID}/sign", json={"provider": "diia"}
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["redirect_url"].startswith("https://diia.example")
    body = harness.posts[0]["body"]
    assert harness.posts[0]["path"] == "/signing/sessions"
    assert body["canonical_json"]["report"]["code"] == "REP-2026-00001"
    # document hash == canonical JCS hash (the envelope binds to it)
    recomputed = hashlib.sha256(canonicalize(body["canonical_json"])).hexdigest()
    assert body["document_pdf_hash_hex"] == recomputed


def test_signing_service_errors_pass_through(harness) -> None:
    _stub_report(harness, status=ReportStatus.FINALIZED)
    harness.install(HTTPException(401, detail={"error": "account_password_rejected"}))
    resp = harness.client.post(
        f"/v1/reports/{REPORT_ID}/sign",
        json={"provider": "dev_password", "password": "wrong"},
    )
    assert resp.status_code == 401


def test_amendment_signs_as_amendment(harness) -> None:
    _stub_report(
        harness,
        status=ReportStatus.SIGNED,
        version=_version_row(is_amendment=True, signed_at=None),
    )
    harness.install(_inline_result() | {"report_status": "amended"})
    resp = harness.client.post(
        f"/v1/reports/{REPORT_ID}/sign",
        json={"provider": "dev_password", "password": "x"},
    )
    assert resp.status_code == 200
    assert harness.posts[0]["body"]["resource_type"] == "amendment"
    assert resp.json()["report_status"] == "amended"
